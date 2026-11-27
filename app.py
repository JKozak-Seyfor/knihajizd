import base64
import json
import re
import time
from datetime import date
from urllib.parse import urlsplit

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Kontrola knih jízd | Seyfor", page_icon="🚙", layout="wide")
GITHUB_FILE = "data/spravci.json"
CONTACTS_FILE = "data/kontakty.json"
MONTH_NAMES = ["Leden", "Únor", "Březen", "Duben", "Květen", "Červen", "Červenec", "Srpen", "Září", "Říjen", "Listopad", "Prosinec"]


def github_config():
    keys = ("GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO", "GITHUB_BRANCH")
    try:
        cfg = {key: str(st.secrets[key]).strip() for key in keys}
        if not all(cfg.values()):
            raise ValueError("Prázdná hodnota konfigurace GitHubu.")
        return cfg
    except (KeyError, ValueError, Exception) as exc:
        # Nevypisovat hodnoty konfigurace ani token.
        raise RuntimeError("Chybí nebo je neplatná konfigurace GitHubu ve Streamlit Secrets.") from None


def github_file_url(cfg, file_path=GITHUB_FILE):
    from urllib.parse import quote
    owner = quote(cfg["GITHUB_OWNER"], safe="")
    repo = quote(cfg["GITHUB_REPO"], safe="")
    path = "/".join(quote(p, safe="") for p in file_path.split("/"))
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"


def github_headers(cfg):
    return {
        "Authorization": f"Bearer {cfg['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fetch_managers():
    cfg = github_config()
    response = requests.get(
        github_file_url(cfg),
        headers=github_headers(cfg),
        params={"ref": cfg["GITHUB_BRANCH"]},
        timeout=(10, 30),
    )
    response.raise_for_status()
    file_data = response.json()
    if file_data.get("type") != "file" or not file_data.get("sha"):
        raise ValueError("GitHub nevrátil očekávaný soubor a jeho SHA.")
    content = base64.b64decode(file_data["content"], validate=False).decode("utf-8")
    managers = json.loads(content)
    if not isinstance(managers, dict):
        raise ValueError("data/spravci.json musí obsahovat JSON objekt.")
    result = {}
    for name, email in managers.items():
        name, email = str(name).strip(), str(email).strip().lower()
        if not name or not valid_email(email):
            raise ValueError("data/spravci.json obsahuje neplatné jméno nebo e-mail.")
        result[name] = email
    return result, file_data["sha"]


def load_managers():
    try:
        managers, sha = fetch_managers()
        st.session_state["managers_sha"] = sha
        return managers
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        st.error(f"GitHub seznam správců nelze načíst (HTTP {status}). Zkontroluj repozitář, větev a oprávnění tokenu.")
    except (requests.exceptions.RequestException, ValueError, RuntimeError, KeyError) as exc:
        st.error(f"GitHub seznam správců nelze načíst: {type(exc).__name__}. Zkontroluj konfiguraci a soubor.")
    return None


def save_managers(managers, expected_sha):
    try:
        cfg = github_config()
        content = (json.dumps(managers, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        response = requests.put(
            github_file_url(cfg),
            headers=github_headers(cfg),
            json={
                "message": "Aktualizace seznamu správců knih jízd",
                "content": base64.b64encode(content).decode("ascii"),
                "sha": expected_sha,
                "branch": cfg["GITHUB_BRANCH"],
            },
