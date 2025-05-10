import requests
import time
import logging
from web3 import Web3

logger = logging.getLogger("eth_bridge")

class EthBridge:
    def __init__(self, account, source_chain_id, target_chain_id, amount_wei, rpc_handler):
        """
        account: dict с ключами 'address' и 'private_key'
        source_chain_id: int (например, 1 для Ethereum mainnet)
        target_chain_id: int (например, 10 для Optimism)
        amount_wei: int (сумма в wei)
        rpc_handler: объект с методами get_w3(), send_transaction_with_retry(), wait_for_receipt_with_retry()
        """
        self.account = account
        self.source_chain_id = source_chain_id
        self.target_chain_id = target_chain_id
        self.amount_wei = amount_wei
        self.rpc_handler = rpc_handler

    def get_quote(self):
        for _ in range(3):
            try:
                response = requests.post(
                    'https://api.relay.link/quote',
                    headers={
                        'accept': 'application/json',
                        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36'
                    },
                    json={
                        'user': self.account["address"].lower(),
                        'originChainId': self.source_chain_id,
                        'destinationChainId': self.target_chain_id,
                        'originCurrency': '0x0000000000000000000000000000000000000000',
                        'destinationCurrency': '0x0000000000000000000000000000000000000000',
                        'recipient': self.account["address"],
                        'tradeType': 'EXACT_INPUT',
                        'amount': str(self.amount_wei),
                        'referrer': 'relay.link/swap',
                        'slippageTolerance': '',
                        'useExternalLiquidity': False
                    },
                    timeout=30
                )
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.warning(f"Quote error: {str(e)}, retrying...")
                time.sleep(2)
        return None

    def execute_bridge(self, quote_data):
        w3 = self.rpc_handler.get_w3()
        tx_data = quote_data['steps'][0]['items'][0]['data']

        logger.info(f"Received tx_data: {tx_data}")
        logger.info(f"Bridging from {self.source_chain_id} to {self.target_chain_id}")

        # Явно заменяем chainId в tx_data на chainId сети-источника
        tx_data['chainId'] = self.source_chain_id

        tx_params = {
            'chainId': self.source_chain_id,
            'to': Web3.to_checksum_address(tx_data['to']),
            'value': int(tx_data['value']),
            'data': tx_data['data'],
            'gas': int(tx_data.get('gas', 100000)),
            'maxFeePerGas': int(tx_data.get('maxFeePerGas', w3.eth.gas_price)),
            'maxPriorityFeePerGas': int(tx_data.get('maxPriorityFeePerGas', w3.to_wei(0.01, 'gwei'))),
            'nonce': w3.eth.get_transaction_count(self.account["address"], 'pending')
        }

        try:
            private_key = self.account["private_key"]
            tx_hash = self.rpc_handler.send_transaction_with_retry(tx_params, private_key, w3)
            receipt = self.rpc_handler.wait_for_receipt_with_retry(tx_hash, w3)
            return receipt.status == 1
        except Exception as e:
            logger.error(f"Bridge execution failed: {str(e)}")
            return False
