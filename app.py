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


def github_file_url(cfg):
    from urllib.parse import quote
    owner = quote(cfg["GITHUB_OWNER"], safe="")
    repo = quote(cfg["GITHUB_REPO"], safe="")
    path = "/".join(quote(p, safe="") for p in GITHUB_FILE.split("/"))
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
            timeout=(10, 30),
        )
        if response.status_code in (409, 422):
            st.error("GitHub změnu odmítl. Seznam mohl mezitím změnit jiný uživatel. Obnov stránku a akci zopakuj.")
            return False
        response.raise_for_status()
        return True
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        st.error(f"Zápis na GitHub selhal (HTTP {status}). Ověř Contents: Read and write a případná pravidla ochrany větve.")
    except (requests.exceptions.RequestException, RuntimeError) as exc:
        st.error(f"Zápis na GitHub selhal ({type(exc).__name__}).")
    return False


def valid_email(email):
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email))


def load_endpoint():
    try:
        return str(st.secrets["POWER_AUTOMATE_URL"]).strip()
    except Exception:
        return ""


def normalize_findings(payload):
    rows = []
    for finding in payload.get("findings", []):
        if not isinstance(finding, dict):
            continue
        rows.append({
            "Soubor": finding.get("file", ""),
            "Závažnost": finding.get("severity", ""),
            "Pravidlo": finding.get("rule", ""),
            "Řádek": finding.get("row", ""),
            "Popis": finding.get("message", ""),
        })
    return rows


def is_safe_poll_url(location, endpoint):
    """Restrict polling to HTTPS and the same origin as the configured flow endpoint.

    Some Power Automate configurations may return a different Microsoft-owned
    status host; do not silently permit it. Review and explicitly allow it if needed.
    """
    try:
        poll = urlsplit(location)
        trigger = urlsplit(endpoint)
        return (
            poll.scheme == "https"
            and trigger.scheme == "https"
            and poll.hostname is not None
            and poll.hostname.lower() == trigger.hostname.lower()
            and (poll.port or 443) == (trigger.port or 443)
            and not poll.username and not poll.password
            and not poll.fragment
        )
    except ValueError:
        return False


def parse_result(response):
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("Power Automate nevrátilo platný JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Odpověď Power Automate není JSON objekt.")
    return payload


def run_flow_async(endpoint, body, status_box):
    """Return final JSON, supporting 202/Location polling where available."""
    with requests.Session() as session:
        response = session.post(endpoint, json=body, timeout=(15, 90), allow_redirects=False)
        status_box.info(f"Počáteční HTTP status: {response.status_code}")
        response.raise_for_status()

        if response.status_code == 200:
            return parse_result(response)
        if response.status_code != 202:
            raise ValueError(f"Neočekávaný HTTP status {response.status_code}.")

        location = response.headers.get("Location")
        if not location:
            raise ValueError("Flow vrátilo 202 bez hlavičky Location. Zkontroluj nastavení Asynchronous response.")
        if not is_safe_poll_url(location, endpoint):
            raise ValueError(
                "Polling URL z hlavičky Location má jiný host nebo nevyhovuje bezpečnostním pravidlům. "
                "Je potřeba ověřit její doménu a výslovně ji povolit v kódu."
            )

        status_box.info("HTTP 202: požadavek přijat. Čekám na dokončení kontroly…")
        deadline = time.monotonic() + 900
        attempts = 0
        while time.monotonic() < deadline:
            retry_after = response.headers.get("Retry-After", "5")
            try:
                delay = max(2, min(int(retry_after), 30))
            except (TypeError, ValueError):
                delay = 5
            time.sleep(delay)
            attempts += 1
            response = session.get(location, timeout=(15, 60), allow_redirects=False)
            if response.status_code == 202:
                status_box.info(f"Kontrola stále probíhá (dotaz č. {attempts}).")
                continue
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError(f"Neočekávaný status při načítání výsledku: {response.status_code}.")
            status_box.success("Kontrola dokončena. Výsledek byl načten.")
            return parse_result(response)
        raise TimeoutError("Kontrola nebyla dokončena do 15 minut. Flow může stále běžet.")


st.title("Kontrola knih jízd")
st.caption("Seyfor · Interní demonstrační aplikace")
st.info("Výběr správce není ověření identity. Aplikace neposkytuje oddělení přístupu k výsledkům podle uživatele.")

managers = load_managers()
endpoint = load_endpoint()
tab_check, tab_admin = st.tabs(["🚙 Kontrola knih jízd", "👥 Správa správců"])

with tab_check:
    st.subheader("Spustit kontrolu")
    if not endpoint:
        st.error("Chybí POWER_AUTOMATE_URL ve Streamlit Secrets.")
    if managers == {}:
        st.info("Nejprve přidej správce v záložce Správa správců.")

    now = date.today()
    years = list(range(now.year - 3, now.year + 2))
    if endpoint and managers:
        with st.form("logbook_check"):
            col1, col2, col3 = st.columns([2.1, 1, 1])
            with col1:
                selected = st.selectbox("Správce knih jízd", list(managers), format_func=lambda name: f"{name} — {managers[name]}")
            with col2:
                month = st.selectbox("Měsíc", range(1, 13), index=now.month - 1, format_func=lambda m: MONTH_NAMES[m - 1])
            with col3:
                year = st.selectbox("Rok", years, index=years.index(now.year))
            submitted = st.form_submit_button("Spustit kontrolu", type="primary", use_container_width=True)

        if submitted:
            period = f"{year}-{month:02d}"
            expected_email = managers[selected]
            st.session_state.pop("latest_result", None)
            st.session_state.pop("latest_manager", None)
            status_box = st.empty()
            try:
                with st.spinner("Probíhá kontrola knih jízd…"):
                    result = run_flow_async(endpoint, {"period": period, "adminEmail": expected_email}, status_box)
                response_email = str(result.get("adminEmail") or "").strip().lower()
                response_period = str(result.get("period") or "").strip()
                if response_email != expected_email or response_period != period:
                    st.error(
                        "Výsledek neodpovídá požadovanému správci nebo období. "
                        "Ověř, že flow vrací finální JSON s adminEmail a period. "
                        "Výsledky nebyly zobrazeny."
                    )
                    st.caption(f"Přítomnost polí: adminEmail={'ano' if 'adminEmail' in result else 'ne'}, period={'ano' if 'period' in result else 'ne'}")
                elif not isinstance(result.get("findings"), list):
                    st.error("Výsledná odpověď neobsahuje pole findings ve formátu seznamu.")
                else:
                    st.session_state["latest_result"] = result
                    st.session_state["latest_manager"] = selected
                    st.success("Výsledky kontroly jsou připraveny.")
            except requests.exceptions.Timeout:
                st.error("Vypršel časový limit HTTP požadavku. Zkontroluj historii běhu flow před opakováním.")
            except requests.exceptions.RequestException as exc:
                st.error(f"HTTP komunikace selhala ({type(exc).__name__}). Zkontroluj běh flow.")
            except (ValueError, TimeoutError) as exc:
                st.error(str(exc))

    result = st.session_state.get("latest_result")
    if result:
        st.divider()
        st.subheader("Výsledky poslední kontroly")
        st.caption(f"Správce: {st.session_state.get('latest_manager', '—')} | Období: {result.get('period', '—')} | RunID: {result.get('runId', '—')}")
        a, b, c = st.columns(3)
        a.metric("Zkontrolované knihy", result.get("filesChecked", "—"))
        b.metric("Chyby", result.get("errorsCount", "—"))
        c.metric("Varování", result.get("warningsCount", "—"))
        findings = result.get("findings", [])
        if not findings:
            st.success("Kontrola nevrátila žádné nálezy.")
        else:
            rows = normalize_findings(result)
            for filename in sorted({str(row["Soubor"]) for row in rows}):
                file_rows = [row for row in rows if str(row["Soubor"]) == filename]
                with st.expander(f"{filename or 'Neuvedený soubor'} · {len(file_rows)} nálezů", expanded=True):
                    st.dataframe(pd.DataFrame(file_rows), hide_index=True, use_container_width=True)
            st.download_button("Stáhnout nálezy jako CSV", pd.DataFrame(rows).to_csv(index=False).encode("utf-8-sig"), file_name=f"nalez_{result.get('period', 'kontrola')}.csv", mime="text/csv")
        st.download_button("Stáhnout JSON odpověď", json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"), file_name=f"kontrola_{result.get('period', 'vysledek')}.json", mime="application/json")

with tab_admin:
    st.subheader("Správa správců")
    st.caption("Seznam se čte a zapisuje do data/spravci.json na GitHubu. Token je uložen ve Streamlit Secrets.")
    if managers is not None and managers:
        st.dataframe(pd.DataFrame([{"Správce": name, "Pracovní e-mail": email} for name, email in sorted(managers.items())]), hide_index=True, use_container_width=True)
    elif managers == {}:
        st.info("Zatím není přidán žádný správce.")

    st.markdown("### Přidat správce")
    with st.form("add_manager", clear_on_submit=True):
        new_name = st.text_input("Jméno správce", placeholder="Jan Marschik")
        new_email = st.text_input("Pracovní e-mail", placeholder="jan.marschik@seyfor.com")
        add_manager = st.form_submit_button("Přidat správce", type="primary")
    if add_manager and managers is not None:
        name, email = new_name.strip(), new_email.strip().lower()
        if not name:
            st.error("Vyplň jméno správce.")
        elif not valid_email(email):
            st.error("Zadej platnou e-mailovou adresu.")
        elif name.casefold() in (n.casefold() for n in managers):
            st.error("Správce s tímto jménem již existuje.")
        elif email in managers.values():
            st.error("Tento e-mail je již přiřazen jinému správci.")
        elif save_managers({**managers, name: email}, st.session_state["managers_sha"]):
            st.rerun()

    if managers is not None and managers:
        st.divider()
        st.markdown("### Odebrat správce")
        remove_name = st.selectbox("Vyber správce k odebrání", sorted(managers), key="remove_manager")
        st.caption(f"E-mail: {managers[remove_name]}")
        confirm_remove = st.checkbox("Potvrzuji odebrání vybraného správce")
        if st.button("Odebrat správce", disabled=not confirm_remove):
            updated = dict(managers)
            del updated[remove_name]
            if save_managers(updated, st.session_state["managers_sha"]):
                st.rerun()
    st.warning("Správa správců není chráněna přihlášením. Každý s přístupem k aplikaci může seznam měnit.")
    st.caption("Změny se commitují do GitHubu. Souběžné změny jsou chráněny kontrolou SHA; při konfliktu obnov stránku.")
