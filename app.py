import calendar
import json
from datetime import date
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Kontrola knih jízd | Seyfor", page_icon="🚙", layout="wide")


def load_managers():
    try:
        data = st.secrets.get("managers", {})
        return {name: str(email).strip().lower() for name, email in data.items() if str(email).strip()}
    except Exception:
        return {}


def load_endpoint():
    try:
        return str(st.secrets["POWER_AUTOMATE_URL"])
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


st.title("Kontrola knih jízd")
st.caption("Seyfor · Interní demonstrační aplikace")
st.info("Vyber správce a období. Kontrola zpracuje knihy přiřazené vybranému správci. Výběr správce není ověření identity uživatele.")

managers = load_managers()
endpoint = load_endpoint()
if not endpoint:
    st.error("Chybí konfigurace POWER_AUTOMATE_URL v nastavení Secrets aplikace.")
    st.stop()
if not managers:
    st.error("Chybí seznam správců v konfiguraci Secrets (`[managers]`).")
    st.stop()

now = date.today()
years = list(range(now.year - 3, now.year + 2))
month_names = ["Leden", "Únor", "Březen", "Duben", "Květen", "Červen", "Červenec", "Srpen", "Září", "Říjen", "Listopad", "Prosinec"]

with st.form("logbook_check"):
    col1, col2, col3 = st.columns([2.1, 1, 1])
    with col1:
        selected = st.selectbox("Správce knih jízd", list(managers), format_func=lambda name: f"{name} — {managers[name]}")
    with col2:
        month = st.selectbox("Měsíc", range(1, 13), index=now.month - 1, format_func=lambda m: month_names[m - 1])
    with col3:
        year = st.selectbox("Rok", years, index=years.index(now.year))
    submitted = st.form_submit_button("Spustit kontrolu", type="primary", use_container_width=True)

if submitted:
    period = f"{year}-{month:02d}"
    body = {"period": period, "adminEmail": managers[selected]}
    try:
        with st.spinner("Probíhá kontrola knih jízd. Zpracování může trvat několik minut…"):
            # Trigger URL and its signing token never reach the browser.
            response = requests.post(endpoint, json=body, timeout=(15, 240))
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Odpověď není JSON objekt.")
        if result.get("adminEmail", "").strip().lower() != managers[selected] or result.get("period") != period:
            st.error("Odpověď neodpovídá požadovanému správci nebo období. Výsledky nebyly zobrazeny.")
            st.stop()
        st.session_state["latest_result"] = result
        st.session_state["latest_manager"] = selected
    except requests.exceptions.Timeout:
        st.error("Časový limit požadavku vypršel. Flow může stále pokračovat; před opakováním zkontroluj historii spuštění, aby nevznikly duplicitní zápisy.")
    except requests.exceptions.RequestException as exc:
        st.error(f"Volání Power Automate selhalo ({type(exc).__name__}). Zkontroluj historii spuštění flow a nastavení endpointu.")
    except (ValueError, json.JSONDecodeError):
        st.error("Flow nevrátilo očekávanou JSON odpověď.")

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
    if not isinstance(findings, list):
        st.error("Neplatný formát nálezů v odpovědi.")
    elif not findings:
        st.success("Kontrola nevrátila žádné nálezy.")
    else:
        rows = normalize_findings(result)
        for filename in sorted(set(r["Soubor"] for r in rows)):
            with st.expander(f"{filename} · {sum(r['Soubor'] == filename for r in rows)} nálezů", expanded=True):
                st.dataframe(pd.DataFrame([r for r in rows if r["Soubor"] == filename]), hide_index=True, use_container_width=True)
        st.download_button("Stáhnout nálezy jako CSV", pd.DataFrame(rows).to_csv(index=False).encode("utf-8-sig"), file_name=f"nalez_{result.get('period', 'kontrola')}.csv", mime="text/csv")
    st.download_button("Stáhnout JSON odpověď", json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"), file_name=f"kontrola_{result.get('period', 'vysledek')}.json", mime="application/json")
