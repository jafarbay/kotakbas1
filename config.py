# Конфиг для деплоя и бриджа

NETWORKS = {
    "Base": {
        "chain_id": 8453,
        "rpcs": [
            "",
            "https://rpc.ankr.com/base"
            "https://mainnet.base.org",
            "https://8453.rpc.thirdweb.com"
        ],
        "deploy_count": 100,  # сколько контрактов деплоить
        "deploy_probability": 0.3  # вероятность деплоя в этой сети
    },
    "optimism": {
        "chain_id": 10,
        "rpcs": [
            "",
            "https://optimism.llamarpc.com",
            "https://optimism.drpc.org",
            "https://rpc.ankr.com/optimism",
            "https://10.rpc.thirdweb.com"
        ],
        "deploy_count": 100,
        "deploy_probability": 0.2
    },
    "UniChain": {
        "chain_id": 130,
        "rpcs": [
            ""
        ],
        "deploy_count": 100,
        "deploy_probability": 0.2
    },
    "Mode": {
        "chain_id": 34443,
        "rpcs": [
            "https://1rpc.io/mode",
            "https://rpc.ankr.com/mode",
            "https://mode.drpc.org",
            "https://mainnet.mode.network",
            "https://mode.gateway.tenderly.co",
            "https://34443.rpc.thirdweb.com"
        ],
        "deploy_count": 30,
        "deploy_probability": 0.2
    },
    "ink": {
        "chain_id": 57073,
        "rpcs": [
            "https://rpc-qnd.inkonchain.com",
            "https://ink.drpc.org",
            "https://rpc-gel.inkonchain.com",
            "https://57073.rpc.thirdweb.com"
        ],
        "deploy_count": 100,
        "deploy_probability": 0.2
    },
    "Lisk": {
        "chain_id": 1135,
        "rpcs": [
            "https://lisk.drpc.org",
            "https://lisk.gateway.tenderly.co",
            "https://rpc.api.lisk.com",
            "https://1135.rpc.thirdweb.com"
        ],
        "deploy_count": 100,
        "deploy_probability": 0.2
    },
    "Soneium": {
        "chain_id": 1868,
        "rpcs": [
            "https://soneium.drpc.org",
            "https://rpc.soneium.org",
            "https://soneium.drpc.org",
            "https://1868.rpc.thirdweb.com"
        ],
        "deploy_count": 150,
        "deploy_probability": 0.2
    }

}

# Задержка между деплоями (секунды)
DEPLOY_DELAY_RANGE = (60, 120)  # от 30 до 120 секунд

# Пример использования:
# from config import NETWORKS, DEPLOY_DELAY_RANGE 
