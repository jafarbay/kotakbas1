import json
import os
import random

DB_PATH = "wallets_db.json"

def load_db():
    if not os.path.exists(DB_PATH):
        return {}
    with open(DB_PATH, "r") as f:
        return json.load(f)

def save_db(db):
    with open(DB_PATH, "w") as f:
        json.dump(db, f, indent=2)

def generate_route(networks):
    actions = []
    # Счётчики оставшихся deploy по сетям
    deploy_left = {network: cfg["deploy_count"] for network, cfg in networks.items()}
    total_deploys = sum(deploy_left.values())
    total_interacts = total_deploys
    created_contracts = []  # список {network, contract_index}
    deploys_done = 0
    interacts_done = 0
    # Первое действие всегда deploy
    available_networks = [n for n in deploy_left if deploy_left[n] > 0]
    first_network = random.choice(available_networks)
    actions.append({"network": first_network, "action": "deploy", "contract_index": 1})
    deploy_left[first_network] -= 1
    deploys_done += 1
    created_contracts.append({"network": first_network, "contract_index": 1})
    # Основной цикл
    while deploys_done < total_deploys or interacts_done < total_interacts:
        possible = []
        # Можно делать deploy, если остались
        available_networks = [n for n in deploy_left if deploy_left[n] > 0]
        if available_networks:
            possible.append("deploy")
        # Можно делать interact, если есть созданные контракты и ещё не все interact
        if created_contracts and interacts_done < total_interacts:
            possible.append("interact")
        if not possible:
            break  # всё сделано
        action = random.choice(possible)
        if action == "deploy":
            network = random.choice(available_networks)
            contract_index = networks[network]["deploy_count"] - deploy_left[network] + 1
            actions.append({"network": network, "action": "deploy", "contract_index": contract_index})
            deploy_left[network] -= 1
            deploys_done += 1
            created_contracts.append({"network": network, "contract_index": contract_index})
        else:  # interact
            contract = random.choice(created_contracts)
            actions.append({
                "network": contract["network"],
                "action": "interact",
                "contract_index": contract["contract_index"]
            })
            interacts_done += 1
    return actions

def get_or_create_wallet(address, networks):
    db = load_db()
    if address not in db:
        route = generate_route(networks)
        db[address] = {
            "route": route,
            "current_index": 0,
            "history": [],
            "deployed_contracts": {}  # network_name: [contract_address, ...]
        }
        save_db(db)
    return db[address]

def update_wallet(address, wallet_data):
    db = load_db()
    db[address] = wallet_data
    save_db(db)
