import json
import re
import time
from datetime import date
from urllib.parse import urlsplit

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Kontrola knih jízd | Seyfor", page_icon="🚙", layout="wide")
MONTH_NAMES = ["Leden", "Únor", "Březen", "Duben", "Květen", "Červen", "Červenec", "Srpen", "Září", "Říjen", "Listopad", "Prosinec"]


def normalize_email(value):
    return str(value or "").strip().lower()


def configured_url(key):
    try:
        raw = st.secrets.get(key, "")
    except (FileNotFoundError, KeyError):
        return ""
    return raw.strip() if isinstance(raw, str) else ""


def parse_admins(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("admins"), list):
        raise ValueError("Odpověď čtecího flow musí obsahovat pole admins.")
    admins = {}
    seen = set()
    for item in payload["admins"]:
        if not isinstance(item, dict):
            raise ValueError("Seznam správců obsahuje neplatný záznam.")
        email, name = item.get("adminEmail"), item.get("displayName")
        if not isinstance(email, str) or not isinstance(name, str):
            raise ValueError("V seznamu správců chybí jméno nebo e-mail.")
        email, name = email.strip(), name.strip()
        normalized = normalize_email(email)
        if not name or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("V seznamu správců je neplatné jméno nebo e-mail.")
        if normalized in seen:
            raise ValueError("Seznam správců obsahuje duplicitní e-mail.")
        seen.add(normalized)
        admins[email] = name
    return dict(sorted(admins.items(), key=lambda pair: (pair[1].casefold(), pair[0].casefold())))


def fetch_admins(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment):
            raise ValueError
    except ValueError:
        raise ValueError("URL čtecího flow musí být platná adresa HTTPS.") from None
    try:
        response = requests.post(url, json={}, timeout=(5, 15), allow_redirects=False)
    except requests.exceptions.RequestException:
        raise RuntimeError("Čtecí flow není dostupné.") from None
    if response.status_code != 200:
        raise RuntimeError(f"Čtecí flow vrátilo HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError:
        raise ValueError("Čtecí flow nevrátilo platný JSON.") from None
    return parse_admins(payload)


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
        deadline = time.monotonic() + 3600
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
        raise TimeoutError("Kontrola nebyla dokončena do 60 minut. Flow může stále běžet.")


st.title("Kontrola knih jízd")
st.caption("Seyfor · Testovací rozhraní pro pověřenou obsluhu")
st.info("Správce se upravují v SharePointu. Výběr správce neověřuje jeho identitu.")

reader_url = configured_url("POWER_AUTOMATE_READER_URL")
if not reader_url:
    st.error("Chybí POWER_AUTOMATE_READER_URL ve Streamlit Secrets.")
    st.stop()

try:
    managers = fetch_admins(reader_url)
except (ValueError, RuntimeError) as exc:
    st.error(str(exc))
    st.stop()

if not managers:
    st.info("V SharePointu nejsou žádní aktivní správci.")
    st.stop()

def admin_label(email):
    name = managers[email]
    return f"{name} ({email})" if list(managers.values()).count(name) > 1 else name

selected = st.selectbox(
    "Správce knih jízd",
    options=list(managers),
    index=None,
    placeholder="Vyber správce",
    format_func=admin_label,
    key="direct_admin",
)
if st.session_state.get("selection_context") != selected:
    st.session_state.pop("latest_result", None)
    st.session_state.pop("latest_manager", None)
    st.session_state["selection_context"] = selected

# Require a separately identified test checker. No production endpoint fallback.
endpoint = configured_url("POWER_AUTOMATE_CHECK_TEST_URL")
if not endpoint:
    st.info("Režim pouze pro čtení. Kontrolní flow se nespouští.")
    st.stop()

now = date.today()
years = list(range(now.year - 3, now.year + 2))
with st.form("logbook_check"):
    col1, col2 = st.columns(2)
    with col1:
        month = st.selectbox("Měsíc", range(1, 13), index=now.month - 1, format_func=lambda m: MONTH_NAMES[m - 1])
    with col2:
        year = st.selectbox("Rok", years, index=years.index(now.year))
    submitted = st.form_submit_button(
        "Spustit kontrolu", type="primary", use_container_width=True,
        disabled=selected is None,
    )

if submitted and selected is not None:
    period = f"{year}-{month:02d}"
    expected_email = selected
    st.session_state.pop("latest_result", None)
    st.session_state.pop("latest_manager", None)
    status_box = st.empty()
    try:
        with st.spinner("Probíhá kontrola knih jízd…"):
            result = run_flow_async(endpoint, {"period": period, "adminEmail": expected_email}, status_box)
        response_email = str(result.get("adminEmail") or "").strip().lower()
        response_period = str(result.get("period") or "").strip()
        if response_email != normalize_email(expected_email) or response_period != period:
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
            st.session_state["latest_manager"] = managers[selected]
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
