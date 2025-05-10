import json
import random
import time
from web3 import Web3
from solcx import compile_source, install_solc, get_installed_solc_versions
from config import NETWORKS, DEPLOY_DELAY_RANGE
import os
from relay import EthBridge
import logging
import colorlog
import numpy as np
from wallet_db import get_or_create_wallet, update_wallet
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

# --- НАСТРОЙКА МНОГОПОТОЧНОСТИ ---
NUM_THREADS = 2  # Количество потоков (настройте по необходимости)
PRIVATE_KEYS_FILE = "private_keys.txt"  # Файл с приватными ключами (один ключ на строку)
THREAD_LOCK = threading.Lock()  # Замок для синхронизации доступа к общим ресурсам (например, базе данных)

# --- Чтение приватных ключей ---
def load_private_keys():
    try:
        with open(PRIVATE_KEYS_FILE, "r") as f:
            keys = [line.strip() for line in f if line.strip()]
        if not keys:
            logger.critical(f"Файл {PRIVATE_KEYS_FILE} пуст или не содержит валидных ключей!")
            sys.exit(1)
        logger.info(f"Загружено {len(keys)} приватных ключей из {PRIVATE_KEYS_FILE}")
        return keys
    except FileNotFoundError:
        logger.critical(f"Файл {PRIVATE_KEYS_FILE} не найден! Создайте файл с приватными ключами.")
        sys.exit(1)

# --- НАСТРОЙКА ЦВЕТНОГО ЛОГГЕРА ---
handler = colorlog.StreamHandler()
handler.setFormatter(colorlog.ColoredFormatter(
    "%(log_color)s[%(asctime)s] [%(levelname)s]%(reset)s [Thread-%(threadName)s] %(message)s",
    datefmt='%Y-%m-%d %H:%M:%S',
    log_colors={
        'DEBUG':    'cyan',
        'INFO':     'green',
        'WARNING':  'yellow',
        'ERROR':    'red',
        'CRITICAL': 'bold_red',
    }
))
logger = colorlog.getLogger()
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# --- НАСТРОЙКИ АККАУНТА ---
SOLC_VERSION = "0.8.0"
ETH_TO_BRIDGE_RANGE = [0.00003, 0.0001]  # ETH
MAX_ATTEMPTS = 3
GAS_SAFETY_MULTIPLIER = 1.1  # запас на комиссии

# --- ШАБЛОНЫ КОНТРАКТОВ ---
CONTRACT_TEMPLATES = [
    {
        "name": "AdvancedStorage",
        "source": """
        pragma solidity ^{version};
        contract {contract_name} {{
            uint256 public value;
            event ValueChanged(uint256 newValue);
            
            constructor(uint256 _value) {{
                value = _value;
                emit ValueChanged(_value);
            }}
            
            function setValue(uint256 _value) public {{
                value = _value;
                emit ValueChanged(_value);
            }}
        }}
        """,
        "constructor_args": lambda: [random.randint(1, 100)],
        "interaction_fn": "setValue",
        "interaction_args": lambda: [random.randint(101, 200)]
    },
    {
        "name": "Voting",
        "source": """
        pragma solidity ^{version};
        contract {contract_name} {{
            mapping(string => uint256) public votes;
            event Voted(string option, uint256 votes);
            
            function vote(string memory _option) public {{
                votes[_option]++;
                emit Voted(_option, votes[_option]);
            }}
        }}
        """,
        "constructor_args": lambda: [],
        "interaction_fn": "vote",
        "interaction_args": lambda: ["option" + str(random.randint(1, 5))]
    },
    {
        "name": "Lottery",
        "source": """
        pragma solidity ^{version};
        contract {contract_name} {{
            address[] public players;
            event PlayerAdded(address player);
            
            function enter() public payable {{
                require(msg.value >= 0 ether, \"Minimum ETH required\");
                players.push(msg.sender);
                emit PlayerAdded(msg.sender);
            }}
        }}
        """,
        "constructor_args": lambda: [],
        "interaction_fn": "enter",
        "interaction_args": lambda: []
    }
]

def get_eth_balance(w3, address):
    try:
        return w3.eth.get_balance(address)
    except Exception as e:
        logger.error(f"Ошибка получения баланса: {e}")
        return 0

def find_richest_network(networks, address):
    max_balance = 0
    richest = None
    for name, cfg in networks.items():
        rpcs = cfg["rpcs"]
        if not rpcs:
            continue
        for rpc in rpcs:
            w3 = Web3(Web3.HTTPProvider(rpc))
            try:
                bal = get_eth_balance(w3, address)
                logger.info(f"Баланс в сети {name}: {bal/1e18:.6f} ETH")
                if bal > max_balance:
                    max_balance = bal
                    richest = (name, cfg, rpc, bal)
                break  # если баланс получен — не пробуем остальные RPC этой сети
            except Exception as e:
                logger.warning(f"Ошибка при проверке баланса в {name} через {rpc}: {e}")
                continue
    if richest:
        logger.info(f"Самая богатая сеть: {richest[0]} (баланс {richest[3]/1e18:.6f} ETH)")
    else:
        logger.warning("Не удалось найти сеть с положительным балансом!")
    return richest  # (name, cfg, rpc, balance)

class DummyRpcHandler:
    def __init__(self, rpc_url):
        self.rpc_url = rpc_url
    def get_w3(self):
        return Web3(Web3.HTTPProvider(self.rpc_url))
    def send_transaction_with_retry(self, tx_params, private_key, w3):
        for attempt in range(MAX_ATTEMPTS):
            try:
                signed = w3.eth.account.sign_transaction(tx_params, private_key)
                tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
                return tx_hash
            except Exception as e:
                logger.error(f"Ошибка отправки транзакции: {e}, попытка {attempt+1}/{MAX_ATTEMPTS}")
                time.sleep(5)
        raise Exception("Не удалось отправить транзакцию после повторов")
    def wait_for_receipt_with_retry(self, tx_hash, w3):
        for attempt in range(MAX_ATTEMPTS):
            try:
                return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            except Exception as e:
                logger.error(f"Ошибка ожидания receipt: {e}, попытка {attempt+1}/{MAX_ATTEMPTS}")
                time.sleep(5)
        raise Exception("Не удалось дождаться receipt после повторов")

def ensure_balance_for_action(w3, address, min_needed, networks, account, rpc_handler, target_chain_id):
    balance = get_eth_balance(w3, address)
    logger.info(f"Balance in {w3.eth.chain_id}: {balance/1e18:.6f} ETH, Required: {min_needed/1e18:.6f} ETH")
    if balance >= min_needed:
        return True
    logger.warning(f"Недостаточно баланса ({balance/1e18:.6f} ETH), требуется {min_needed/1e18:.6f} ETH. Запускаем бридж...")
    for _ in range(2):  # Retry finding richest network
        richest = find_richest_network(networks, address)
        if richest and richest[3] >= ETH_TO_BRIDGE_RANGE[0] * 1e18:
            break
    if not richest or richest[3] < ETH_TO_BRIDGE_RANGE[0] * 1e18:
        logger.error("Нет сети с достаточным балансом для бриджа!")
        return False
    amount_eth = random.uniform(*ETH_TO_BRIDGE_RANGE)
    amount_eth = min(amount_eth, richest[3] / 1e18)
    amount = int(amount_eth * 1e18)
    logger.info(f"Бриджим {amount/1e18:.6f} ETH из сети {richest[0]} в целевую сеть.")
    bridge_account = {"address": address, "private_key": account.key.hex()}  # Используем account.key.hex()
    richest_rpc = richest[2]
    richest_rpc_handler = DummyRpcHandler(richest_rpc)
    bridge = EthBridge(bridge_account, richest[1]["chain_id"], target_chain_id, amount, richest_rpc_handler)
    for attempt in range(MAX_ATTEMPTS):
        quote = bridge.get_quote()
        if quote and bridge.execute_bridge(quote):
            logger.info("Бридж успешен!")
            for _ in range(24):  # Wait up to 120 seconds
                if get_eth_balance(w3, address) >= min_needed:
                    logger.info("Funds received after bridge")
                    return True
                time.sleep(5)
            logger.error("Баланс не поступил после бриджа!")
            return False
        logger.warning(f"Бридж не удался, повтор {attempt+1}/{MAX_ATTEMPTS}...")
        time.sleep(10)
    return False

def try_build_and_send(w3, Contract, constructor_args, tx_params, account, action_desc, use_legacy=False):
    try:
        tx_params = tx_params.copy()
        if use_legacy:
            tx_params.pop('maxFeePerGas', None)
            tx_params.pop('maxPriorityFeePerGas', None)
            tx_params['gasPrice'] = w3.eth.gas_price
            tx_params['type'] = 0
        construct_txn = Contract.constructor(*constructor_args).build_transaction(tx_params)
        gas_estimate = w3.eth.estimate_gas(construct_txn)
        construct_txn['gas'] = int(gas_estimate * GAS_SAFETY_MULTIPLIER)
        logger.info(f"Подпись транзакции {action_desc}")
        signed = w3.eth.account.sign_transaction(construct_txn, private_key=account.key)  # Используем account.key
        logger.info(f"Отправка транзакции {action_desc}")
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        logger.info(f"Ожидание подтверждения {action_desc}")
        tx_receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        return tx_receipt
    except Exception as e:
        raise e

def deploy_contract(rpc_list, chain_id, networks, network_name, wallet_data, account):
    for rpc_url in rpc_list:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            assert w3.is_connected(), f"Нет соединения с {rpc_url}"
            template = random.choice(CONTRACT_TEMPLATES)
            contract_name = template["name"]
            source_code = template["source"].format(version=SOLC_VERSION, contract_name=contract_name)
            constructor_args = template["constructor_args"]()
            if SOLC_VERSION not in [str(v) for v in get_installed_solc_versions()]:
                install_solc(SOLC_VERSION)
            compiled_sol = compile_source(
                source_code,
                output_values=['abi', 'bin'],
                solc_version=SOLC_VERSION
            )
            contract_id, contract_interface = compiled_sol.popitem()
            abi = contract_interface['abi']
            bytecode = contract_interface['bin']
            Contract = w3.eth.contract(abi=abi, bytecode=bytecode)
            nonce = w3.eth.get_transaction_count(account.address)
            latest_block = w3.eth.get_block('latest')
            base_fee = latest_block.get('baseFeePerGas', w3.to_wei(0.001, 'gwei'))
            try:
                max_priority_fee = w3.eth.max_priority_fee
            except Exception:
                max_priority_fee = w3.to_wei(0.01, 'gwei')
            max_fee_per_gas = base_fee + max_priority_fee * 2
            tx_params = {
                'from': account.address,
                'nonce': nonce,
                'chainId': chain_id,
                'type': 2,
                'maxFeePerGas': max_fee_per_gas,
                'maxPriorityFeePerGas': max_priority_fee,
            }
            min_needed = int(400000 * max_fee_per_gas * GAS_SAFETY_MULTIPLIER)
            if get_eth_balance(w3, account.address) < min_needed:
                logger.warning(f"Недостаточно баланса для деплоя в {network_name}, пробуем бридж...")
                rpc_handler = DummyRpcHandler(rpc_url)
                bridged = ensure_balance_for_action(w3, account.address, min_needed, networks, account, rpc_handler, chain_id)
                if not bridged:
                    logger.error(f'Не удалось обеспечить баланс для деплоя в {network_name}')
                    continue
                if get_eth_balance(w3, account.address) < min_needed:
                    logger.error(f'Баланс всё ещё недостаточен после бриджа в {network_name}')
                    continue
                delay = random.randint(*DEPLOY_DELAY_RANGE)
                logger.info(f"Ожидание {delay} секунд после бриджа перед деплоем...")
                time.sleep(delay)
            tx_receipt = try_build_and_send(w3, Contract, constructor_args, tx_params, account, f"деплоя {contract_name} (EIP-1559)", use_legacy=False)
            logger.info(f"Контракт задеплоен по адресу: {tx_receipt.contractAddress}")
            with THREAD_LOCK:
                wallet_data["deployed_contracts"].setdefault(network_name, []).append({
                    "address": tx_receipt.contractAddress,
                    "template_name": contract_name
                })
                wallet_data["history"].append({
                    "network": network_name,
                    "action": "deploy",
                    "status": "success",
                    "contract_address": tx_receipt.contractAddress,
                    "contract_index": None,
                    "template_name": contract_name
                })
            return tx_receipt.contractAddress, contract_name
        except Exception as e:
            logger.error(f"Ошибка деплоя в {network_name} на RPC {rpc_url}: {e}")
            continue
    return None, None

def interact_with_contract(rpc_list, chain_id, networks, network_name, wallet_data, account, contract_index):
    deployed = wallet_data.get("deployed_contracts", {}).get(network_name, [])
    migrated = False
    for i, c in enumerate(deployed):
        if isinstance(c, str):
            deployed[i] = {"address": c, "template_name": "Unknown"}
            migrated = True
    if migrated:
        logger.warning(f"Выполнена миграция deployed_contracts для {network_name} на новый формат. Старые контракты будут иметь template_name='Unknown'.")
        with THREAD_LOCK:
            wallet_data["deployed_contracts"][network_name] = deployed
            update_wallet(account.address, wallet_data)
    if not deployed or len(deployed) < contract_index:
        logger.warning(f"Нет задеплоенного контракта #{contract_index} в {network_name} для взаимодействия")
        return False
    contract_info = deployed[contract_index - 1]
    contract_address = contract_info["address"]
    template_name = contract_info.get("template_name")
    logger.info(f"Contract #{contract_index} template: {template_name}")
    template = None
    for t in CONTRACT_TEMPLATES:
        if t["name"] == template_name:
            template = t
            break
    if template is None:
        logger.error(f"Не найден шаблон {template_name} для interact в {network_name} контракт #{contract_index}")
        return False
    for rpc_url in rpc_list:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            assert w3.is_connected(), f"Нет соединения с {rpc_url}"
            if SOLC_VERSION not in [str(v) for v in get_installed_solc_versions()]:
                install_solc(SOLC_VERSION)
            abi = compile_source(
                template["source"].format(version=SOLC_VERSION, contract_name=template["name"]),
                output_values=['abi'],
                solc_version=SOLC_VERSION
            ).popitem()[1]['abi']
            contract_instance = w3.eth.contract(address=contract_address, abi=abi)
            interaction_fn = template["interaction_fn"]
            interaction_args = template["interaction_args"]()
            nonce = w3.eth.get_transaction_count(account.address)
            logger.info(f"Preparing interaction with {interaction_fn}, args: {interaction_args}")

            # Always set value to 0 for all interactions
            value = 0
            logger.info(f"Value to send: {value/1e18:.6f} ETH")

            if network_name.lower() in ["lisk", "mode"]:
                gas_price = int(w3.eth.gas_price * 1.1)
                interaction_txn = contract_instance.get_function_by_name(interaction_fn)(*interaction_args).build_transaction({
                    'chainId': w3.eth.chain_id,
                    'gas': 150000,
                    'gasPrice': gas_price,
                    'nonce': nonce,
                    'from': account.address,
                    'value': value
                })
                try:
                    gas_estimate = w3.eth.estimate_gas(interaction_txn)
                    gas_limit = int(gas_estimate * GAS_SAFETY_MULTIPLIER)
                except Exception as e:
                    logger.warning(f"Не удалось оценить газ для {interaction_fn} в {network_name}: {e}, используем 150000")
                    gas_limit = 150000
                min_needed = gas_limit * gas_price

                balance = get_eth_balance(w3, account.address)
                logger.info(f"Balance in {network_name}: {balance/1e18:.6f} ETH, Required: {min_needed/1e18:.6f} ETH")
                if balance < min_needed:
                    logger.warning(f"Недостаточно баланса ({balance/1e18:.6f} ETH), требуется {min_needed/1e18:.6f} ETH для взаимодействия в {network_name}")
                    rpc_handler = DummyRpcHandler(rpc_url)
                    bridged = ensure_balance_for_action(
                        w3, account.address, min_needed, networks,
                        account, rpc_handler, chain_id
                    )
                    if not bridged:
                        logger.error(f"Не удалось обеспечить баланс для взаимодействия в {network_name}")
                        with THREAD_LOCK:
                            wallet_data["history"].append({
                                "network": network_name,
                                "action": "interact",
                                "status": "fail",
                                "contract_index": contract_index,
                                "error": "Insufficient funds"
                            })
                            update_wallet(account.address, wallet_data)
                        return False
                    delay = random.randint(*DEPLOY_DELAY_RANGE)
                    logger.info(f"Ожидание {delay} секунд после бриджа перед взаимодействием...")
                    time.sleep(delay)
                    balance = get_eth_balance(w3, account.address)
                    if balance < min_needed:
                        logger.error(f"Баланс всё ещё недостаточен после бриджа в {network_name}: {balance/1e18:.6f} ETH")
                        with THREAD_LOCK:
                            wallet_data["history"].append({
                                "network": network_name,
                                "action": "interact",
                                "status": "fail",
                                "contract_index": contract_index,
                                "error": "Insufficient funds after bridge"
                            })
                            update_wallet(account.address, wallet_data)
                        return False

                logger.info(f"Формирование транзакции вызова функции {interaction_fn} для {network_name} (Lisk/Mode-style)")
                interaction_txn['gas'] = gas_limit
                try:
                    contract_instance.get_function_by_name(interaction_fn)(*interaction_args).call({'from': account.address, 'value': value})
                except Exception as e:
                    logger.error(f"Симуляция вызова {interaction_fn} не удалась в {network_name}: {e}")
                    continue
                signed = w3.eth.account.sign_transaction(interaction_txn, private_key=account.key)  # Используем account.key
                logger.info(f"Отправка транзакции вызова функции ({network_name} legacy style)")
                tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
                tx_receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
                logger.info(f"Вызов {interaction_fn} успешно выполнен ({network_name} legacy style) с контрактом #{contract_index}")
                return True
            else:
                latest_block = w3.eth.get_block('latest')
                base_fee = latest_block.get('baseFeePerGas', w3.to_wei(0.001, 'gwei'))
                try:
                    max_priority_fee = w3.eth.max_priority_fee
                except Exception:
                    max_priority_fee = w3.to_wei(0.01, 'gwei')
                max_fee_per_gas = base_fee + max_priority_fee * 2
                tx_params = {
                    'from': account.address,
                    'nonce': nonce,
                    'chainId': chain_id,
                    'type': 2,
                    'maxFeePerGas': max_fee_per_gas,
                    'maxPriorityFeePerGas': max_priority_fee,
                    'value': value
                }
                def build_interaction_tx(tx_params, use_legacy):
                    tx_params = tx_params.copy()
                    if use_legacy:
                        tx_params.pop('maxFeePerGas', None)
                        tx_params.pop('maxPriorityFeePerGas', None)
                        tx_params['gasPrice'] = w3.eth.gas_price
                        tx_params['type': 0]
                    return contract_instance.get_function_by_name(interaction_fn)(*interaction_args).build_transaction(tx_params)
                interaction_txn = build_interaction_tx(tx_params, use_legacy=False)
                try:
                    gas_estimate = w3.eth.estimate_gas(interaction_txn)
                    gas_limit = int(gas_estimate * GAS_SAFETY_MULTIPLIER)
                except Exception as e:
                    logger.warning(f"Не удалось оценить газ для {interaction_fn} в {network_name}: {e}, используем 150000")
                    gas_limit = 150000
                min_needed = gas_limit * max_fee_per_gas

                balance = get_eth_balance(w3, account.address)
                logger.info(f"Balance in {network_name}: {balance/1e18:.6f} ETH, Required: {min_needed/1e18:.6f} ETH")
                if balance < min_needed:
                    logger.warning(f"Недостаточно баланса ({balance/1e18:.6f} ETH), требуется {min_needed/1e18:.6f} ETH для взаимодействия в {network_name}")
                    rpc_handler = DummyRpcHandler(rpc_url)
                    bridged = ensure_balance_for_action(
                        w3, account.address, min_needed, networks,
                        account, rpc_handler, chain_id
                    )
                    if not bridged:
                        logger.error(f"Не удалось обеспечить баланс для взаимодействия в {network_name}")
                        with THREAD_LOCK:
                            wallet_data["history"].append({
                                "network": network_name,
                                "action": "interact",
                                "status": "fail",
                                "contract_index": contract_index,
                                "error": "Insufficient funds"
                            })
                            update_wallet(account.address, wallet_data)
                        return False
                    delay = random.randint(*DEPLOY_DELAY_RANGE)
                    logger.info(f"Ожидание {delay} секунд после бриджа перед взаимодействием...")
                    time.sleep(delay)
                    balance = get_eth_balance(w3, account.address)
                    if balance < min_needed:
                        logger.error(f"Баланс всё ещё недостаточен после бриджа в {network_name}: {balance/1e18:.6f} ETH")
                        with THREAD_LOCK:
                            wallet_data["history"].append({
                                "network": network_name,
                                "action": "interact",
                                "status": "fail",
                                "contract_index": contract_index,
                                "error": "Insufficient funds after bridge"
                            })
                            update_wallet(account.address, wallet_data)
                        return False

                try:
                    interaction_txn = build_interaction_tx(tx_params, use_legacy=False)
                    interaction_txn['gas'] = gas_limit
                    try:
                        contract_instance.get_function_by_name(interaction_fn)(*interaction_args).call({'from': account.address, 'value': value})
                    except Exception as e:
                        logger.error(f"Симуляция вызова {interaction_fn} не удалась в {network_name}: {e}")
                        continue
                    logger.info(f"Параметры транзакции: gas={gas_limit}, maxFeePerGas={max_fee_per_gas/1e9:.2f} Gwei, value={value/1e18:.6f} ETH")
                    signed = w3.eth.account.sign_transaction(interaction_txn, private_key=account.key)  # Используем account.key
                    logger.info(f"Отправка транзакции вызова функции (EIP-1559) в {network_name}")
                    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
                    tx_receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
                    logger.info(f"Вызов {interaction_fn} успешно выполнен в {network_name} с контрактом #{contract_index}")
                    return True
                except Exception as e:
                    logger.warning(f"EIP-1559 interact не удался в {network_name}: {e}, пробуем legacy...")
                    try:
                        interaction_txn = build_interaction_tx(tx_params, use_legacy=True)
                        gas_price = w3.eth.gas_price
                        try:
                            gas_estimate = w3.eth.estimate_gas(interaction_txn)
                            gas_limit = int(gas_estimate * GAS_SAFETY_MULTIPLIER)
                        except Exception as e:
                            logger.warning(f"Не удалось оценить газ для legacy в {network_name}: {e}, используем 150000")
                            gas_limit = 150000
                        min_needed = gas_limit * gas_price
                        balance = get_eth_balance(w3, account.address)
                        logger.info(f"Balance in {network_name} (legacy): {balance/1e18:.6f} ETH, Required: {min_needed/1e18:.6f} ETH")
                        if balance < min_needed:
                            logger.warning(f"Недостаточно баланса для legacy ({balance/1e18:.6f} ETH), требуется {min_needed/1e18:.6f} ETH в {network_name}")
                            rpc_handler = DummyRpcHandler(rpc_url)
                            bridged = ensure_balance_for_action(
                                w3, account.address, min_needed, networks,
                                account, rpc_handler, chain_id
                            )
                            if not bridged:
                                logger.error(f"Не удалось обеспечить баланс для legacy в {network_name}")
                                with THREAD_LOCK:
                                    wallet_data["history"].append({
                                        "network": network_name,
                                        "action": "interact",
                                        "status": "fail",
                                        "contract_index": contract_index,
                                        "error": "Insufficient funds"
                                    })
                                    update_wallet(account.address, wallet_data)
                                return False
                            delay = random.randint(*DEPLOY_DELAY_RANGE)
                            logger.info(f"Ожидание {delay} секунд после бриджа перед взаимодействием...")
                            time.sleep(delay)
                            balance = get_eth_balance(w3, account.address)
                            if balance < min_needed:
                                logger.error(f"Баланс всё ещё недостаточен после бриджа для legacy в {network_name}: {balance/1e18:.6f} ETH")
                                with THREAD_LOCK:
                                    wallet_data["history"].append({
                                        "network": network_name,
                                        "action": "interact",
                                        "status": "fail",
                                        "contract_index": contract_index,
                                        "error": "Insufficient funds after bridge"
                                    })
                                    update_wallet(account.address, wallet_data)
                                return False
                        interaction_txn['gas'] = gas_limit
                        try:
                            contract_instance.get_function_by_name(interaction_fn)(*interaction_args).call({'from': account.address, 'value': value})
                        except Exception as e:
                            logger.error(f"Симуляция вызова {interaction_fn} не удалась в {network_name}: {e}")
                            continue
                        logger.info(f"Параметры транзакции (legacy): gas={gas_limit}, gasPrice={gas_price/1e9:.2f} Gwei, value={value/1e18:.6f} ETH")
                        signed = w3.eth.account.sign_transaction(interaction_txn, private_key=account.key)  # Используем account.key
                        logger.info(f"Отправка транзакции вызова функции (legacy) в {network_name}")
                        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
                        tx_receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
                        logger.info(f"Вызов {interaction_fn} успешно выполнен (legacy) в {network_name} с контрактом #{contract_index}")
                        return True
                    except Exception as e2:
                        logger.error(f"Legacy interact тоже не удался в {network_name}: {e2}")
                        continue
        except Exception as e:
            logger.error(f"Ошибка взаимодействия с контрактом #{contract_index} в {network_name} на RPC {rpc_url}: {e}")
            continue
    return False

DB_PATH = "wallets_db.json"

def init_db():
    with THREAD_LOCK:
        if os.path.exists(DB_PATH):
            logger.info(f"База данных {DB_PATH} уже существует.")
        else:
            with open(DB_PATH, "w") as f:
                f.write("{}\n")
            logger.info(f"Создана новая база данных {DB_PATH}.")

def delete_db():
    with THREAD_LOCK:
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
            logger.info(f"База данных {DB_PATH} удалена.")
        else:
            logger.info(f"База данных {DB_PATH} не найдена для удаления.")

# --- Рабочая функция для потока ---
def worker(private_key, thread_name):
    logger.info(f"Запуск потока для кошелька с адресом {Web3().eth.account.from_key(private_key).address}")
    account = Web3().eth.account.from_key(private_key)
    while True:
        with THREAD_LOCK:
            wallet_data = get_or_create_wallet(account.address, NETWORKS)
        route = wallet_data["route"]
        current_index = wallet_data["current_index"]
        if current_index >= len(route):
            logger.info(f"Маршрут для кошелька {account.address} завершён. Генерируем новый маршрут.")
            from wallet_db import generate_route
            with THREAD_LOCK:
                wallet_data["route"] = generate_route(NETWORKS)
                wallet_data["current_index"] = 0
                update_wallet(account.address, wallet_data)
            route = wallet_data["route"]
            current_index = 0
        step = route[current_index]
        network_name = step["network"]
        action = step["action"]
        contract_index = step.get("contract_index")
        net_cfg = NETWORKS[network_name]
        rpc_list = net_cfg["rpcs"]
        chain_id = net_cfg["chain_id"]
        logger.info(f"Шаг {current_index+1}/{len(route)}: {action} в {network_name} для кошелька {account.address}")
        success = False
        if action == "deploy":
            contract_address, contract_name = deploy_contract(rpc_list, chain_id, NETWORKS, network_name, wallet_data, account)
            if contract_address:
                with THREAD_LOCK:
                    wallet_data["deployed_contracts"].setdefault(network_name, []).append({
                        "address": contract_address,
                        "template_name": contract_name
                    })
                    wallet_data["history"].append({
                        "network": network_name,
                        "action": action,
                        "status": "success",
                        "contract_address": contract_address,
                        "contract_index": contract_index,
                        "template_name": contract_name
                    })
                success = True
        elif action == "interact":
            success = interact_with_contract(rpc_list, chain_id, NETWORKS, network_name, wallet_data, account, contract_index)
            with THREAD_LOCK:
                wallet_data["history"].append({
                    "network": network_name,
                    "action": action,
                    "status": "success" if success else "fail",
                    "contract_index": contract_index
                })
                if not success:
                    wallet_data["current_index"] += 1  # Skip to next step on failure
        else:
            logger.warning(f"Неизвестное действие: {action} для кошелька {account.address}")
        if success:
            with THREAD_LOCK:
                wallet_data["current_index"] += 1
                update_wallet(account.address, wallet_data)
        delay = random.randint(*DEPLOY_DELAY_RANGE)
        logger.info(f"Ожидание {delay} секунд до следующего действия для кошелька {account.address}...")
        time.sleep(delay)

if __name__ == "__main__":
    print("Выберите действие:")
    print("1 — Запустить основной цикл (многопоточный)")
    print("2 — Создать новую базу данных кошельков")
    print("3 — Удалить базу данных кошельков")
    print("0 — Выход")
    try:
        choice = input("Ваш выбор: ").strip()
    except EOFError:
        choice = "0"
    if choice == "2":
        init_db()
        exit(0)
    elif choice == "3":
        delete_db()
        exit(0)
    elif choice == "0":
        print("Выход.")
        exit(0)
    elif choice != "1":
        print("Неизвестный выбор. Выход.")
        exit(1)

    # --- Запуск многопоточного выполнения ---
    private_keys = load_private_keys()
    with ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
        futures = []
        for i, key in enumerate(private_keys):
            futures.append(executor.submit(worker, key, f"Thread-{i+1}"))
        # Ожидание завершения всех потоков (в данном случае бесконечный цикл, так как worker работает вечно)
        for future in futures:
            try:
                future.result()
            except Exception as e:
                logger.error(f"Ошибка в потоке: {e}")
