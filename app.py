import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Kontrola knih jízd | Seyfor", page_icon="🚙", layout="wide")


MANAGERS_FILE = Path(__file__).resolve().parent / "spravci.json"


def load_managers():
    if not MANAGERS_FILE.exists():
        return {}
    try:
        data = json.loads(MANAGERS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Seznam správců musí být JSON objekt.")
        return {
            str(name).strip(): str(email).strip().lower()
            for name, email in data.items()
            if str(name).strip() and str(email).strip()
        }
    except (OSError, ValueError, TypeError) as exc:
        st.error(f"Soubor spravci.json nelze načíst: {exc}")
        return {}


def save_managers(managers):
    """Atomický zápis, aby se při přerušení neztratil celý seznam."""
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=MANAGERS_FILE.parent,
            prefix=".spravci-", suffix=".tmp", delete=False
        ) as handle:
            temp_name = handle.name
            json.dump(managers, handle, ensure_ascii=False, indent=2)
            handle.write("\\n")
        os.replace(temp_name, MANAGERS_FILE)
        return True
    except OSError as exc:
        st.error(f"Seznam správců se nepodařilo uložit: {exc}")
        return False
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def valid_email(email):
    return bool(re.fullmatch(r"[^\\s@]+@[^\\s@]+\\.[^\\s@]+", email))



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

tab_check, tab_admin = st.tabs(["Kontrola knih jízd", "Správa správců"])
managers = load_managers()
endpoint = load_endpoint()

with tab_admin:
    st.subheader("Správa správců")
    st.caption("Seznam je uložen v souboru spravci.json vedle aplikace, nikoli v Secrets.")
    if managers:
        st.dataframe(
            pd.DataFrame([{"Správce": name, "Pracovní e-mail": email}
                          for name, email in sorted(managers.items())]),
            hide_index=True, use_container_width=True
        )
    else:
        st.info("Zatím není přidán žádný správce.")

    with st.form("add_manager", clear_on_submit=True):
        new_name = st.text_input("Jméno správce")
        new_email = st.text_input("Pracovní e-mail")
        add_manager = st.form_submit_button("Přidat správce", type="primary")
    if add_manager:
        name = new_name.strip()
        email = new_email.strip().lower()
        if not name or not valid_email(email):
            st.error("Vyplň jméno a platnou e-mailovou adresu.")
        elif name.casefold() in (n.casefold() for n in managers):
            st.error("Správce s tímto jménem již existuje.")
        elif email in managers.values():
            st.error("Tento e-mail je již přiřazen jinému správci.")
        elif save_managers({**managers, name: email}):
            st.success("Správce byl přidán.")
            st.rerun()

    if managers:
        st.divider()
        remove_name = st.selectbox("Správce k odebrání", sorted(managers), key="remove_manager")
        confirm_remove = st.checkbox("Potvrzuji odebrání vybraného správce")
        if st.button("Odebrat správce", disabled=not confirm_remove):
            updated = dict(managers)
            del updated[remove_name]
            if save_managers(updated):
                st.success("Správce byl odebrán.")
                st.rerun()

    st.warning("Tato záložka není chráněna přihlášením. Každý, kdo má přístup k aplikaci, může seznam upravovat.")
    st.caption("Při hostingu s dočasným souborovým systémem nemusí změny přežít restart nebo redeploy.")

with tab_check:
    if not endpoint:
        st.error("Chybí konfigurace POWER_AUTOMATE_URL v Secrets aplikace.")
    if not managers:
        st.info("Nejprve přidej správce v záložce Správa správců.")

    now = date.today()
    years = list(range(now.year - 3, now.year + 2))
    month_names = ["Leden", "Únor", "Březen", "Duben", "Květen", "Červen", "Červenec", "Srpen", "Září", "Říjen", "Listopad", "Prosinec"]

    if managers and endpoint:
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
                else:
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
