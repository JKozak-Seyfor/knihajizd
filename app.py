import json
import os
import re
import tempfile
import time
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st


# ============================================================
# KONFIGURACE
# ============================================================

st.set_page_config(
    page_title="Kontrola knih jízd | Seyfor",
    page_icon="🚙",
    layout="wide",
)

BASE_DIR = Path(__file__).resolve().parent
MANAGERS_FILE = BASE_DIR / "spravci.json"

MONTH_NAMES = [
    "Leden",
    "Únor",
    "Březen",
    "Duben",
    "Květen",
    "Červen",
    "Červenec",
    "Srpen",
    "Září",
    "Říjen",
    "Listopad",
    "Prosinec",
]

POLL_INTERVAL = 5
MAX_WAIT_SECONDS = 900
REQUEST_TIMEOUT = (15, 60)


# ============================================================
# SPRÁVA SPRÁVCŮ
# ============================================================

def load_managers():
    if not MANAGERS_FILE.exists():
        return {}

    try:
        data = json.loads(
            MANAGERS_FILE.read_text(encoding="utf-8")
        )

        if not isinstance(data, dict):
            raise ValueError(
                "Seznam správců musí být JSON objekt."
            )

        return {
            str(name).strip(): str(email).strip().lower()
            for name, email in data.items()
            if str(name).strip() and str(email).strip()
        }

    except (OSError, ValueError, TypeError) as exc:
        st.error(
            f"Soubor spravci.json nelze načíst: {exc}"
        )
        return {}


def save_managers(managers):
    temp_name = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=MANAGERS_FILE.parent,
            prefix=".spravci-",
            suffix=".tmp",
            delete=False,
        ) as handle:

            temp_name = handle.name

            json.dump(
                managers,
                handle,
                ensure_ascii=False,
                indent=2,
            )

            handle.write("\n")

        os.replace(temp_name, MANAGERS_FILE)
        return True

    except OSError as exc:
        st.error(
            f"Seznam správců se nepodařilo uložit: {exc}"
        )
        return False

    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def valid_email(email):
    return bool(
        re.fullmatch(
            r"[^\s@]+@[^\s@]+\.[^\s@]+",
            email,
        )
    )


# ============================================================
# POWER AUTOMATE
# ============================================================

def load_endpoint():
    try:
        return str(
            st.secrets["POWER_AUTOMATE_URL"]
        ).strip()
    except Exception:
        return ""


def validate_poll_url(location, endpoint):
    """
    Ověří, že polling URL používá HTTPS
    a stejný hostitel jako nakonfigurovaný endpoint.

    Pokud Microsoft vrátí jiného hostitele,
    požadavek se z bezpečnostních důvodů zastaví.
    """

    poll_url = urljoin(endpoint, location)

    original = urlparse(endpoint)
    target = urlparse(poll_url)

    if target.scheme.lower() != "https":
        raise ValueError(
            "Polling URL nepoužívá HTTPS."
        )

    if target.hostname != original.hostname:
        raise ValueError(
            "Power Automate vrátilo polling URL "
            "na jiném hostiteli. "
            "Automatické přesměrování bylo "
            "z bezpečnostních důvodů zablokováno."
        )

    if target.port not in (None, 443):
        raise ValueError(
            "Polling URL používá nepovolený port."
        )

    if target.username or target.password:
        raise ValueError(
            "Polling URL obsahuje nepovolené "
            "přihlašovací údaje."
        )

    return poll_url


def parse_json_response(response):
    try:
        result = response.json()
    except ValueError as exc:
        raise ValueError(
            "Power Automate nevrátilo validní JSON."
        ) from exc

    if not isinstance(result, dict):
        raise ValueError(
            "Odpověď Power Automate není JSON objekt."
        )

    return result


def get_retry_interval(response):
    retry_after = response.headers.get("Retry-After")

    if retry_after:
        try:
            seconds = int(retry_after)

            if 1 <= seconds <= 60:
                return seconds

        except ValueError:
            pass

    return POLL_INTERVAL


def run_flow_async(endpoint, body, status_placeholder):
    """
    Spustí Power Automate.

    HTTP 200:
        Vrátí finální JSON.

    HTTP 202 + Location:
        Pravidelně kontroluje dokončení.

    HTTP 202 bez Location:
        Vyvolá chybu.
    """

    with requests.Session() as session:

        status_placeholder.info(
            "Odesílám požadavek do Power Automate…"
        )

        response = session.post(
            endpoint,
            json=body,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )

        initial_status = response.status_code

        if 300 <= initial_status < 400:
            raise ValueError(
                "Power Automate vrátilo přesměrování "
                "místo očekávané odpovědi."
            )

        response.raise_for_status()

        if initial_status != 202:
            status_placeholder.success(
                f"Power Automate odpovědělo "
                f"HTTP {initial_status}."
            )

            return parse_json_response(response)

        location = response.headers.get("Location")

        if not location:
            raise ValueError(
                "Power Automate vrátilo HTTP 202, "
                "ale odpověď neobsahuje hlavičku Location. "
                "Nelze zjistit stav asynchronního běhu."
            )

        poll_url = validate_poll_url(
            location,
            endpoint,
        )

        status_placeholder.info(
            "Power Automate přijalo požadavek "
            "(HTTP 202). Čekám na dokončení kontroly…"
        )

        deadline = time.monotonic() + MAX_WAIT_SECONDS
        poll_count = 0

        while time.monotonic() < deadline:

            interval = get_retry_interval(response)

            remaining = deadline - time.monotonic()

            if remaining <= 0:
                break

            time.sleep(
                min(interval, remaining)
            )

            poll_count += 1

            elapsed = (
                MAX_WAIT_SECONDS
                - max(
                    0,
                    deadline - time.monotonic()
                )
            )

            status_placeholder.info(
                "Kontrola stále probíhá… "
                f"Uplynulo {int(elapsed)} sekund. "
                f"Dotaz na stav #{poll_count}."
            )

            response = session.get(
                poll_url,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )

            if 300 <= response.status_code < 400:
                raise ValueError(
                    "Polling endpoint vrátil "
                    "nepovolené přesměrování."
                )

            response.raise_for_status()

            if response.status_code == 202:

                next_location = response.headers.get(
                    "Location"
                )

                if next_location:
                    poll_url = validate_poll_url(
                        next_location,
                        endpoint,
                    )

                continue

            result = parse_json_response(response)

            status_placeholder.success(
                "Power Automate dokončilo kontrolu. "
                f"Finální HTTP status: "
                f"{response.status_code}."
            )

            return result

        raise TimeoutError(
            "Kontrola nebyla dokončena do 15 minut. "
            "Flow může stále pokračovat. "
            "Zkontroluj historii běhu Power Automate."
        )


# ============================================================
# ZPRACOVÁNÍ VÝSLEDKŮ
# ============================================================

def normalize_findings(payload):
    rows = []

    findings = payload.get("findings", [])

    if not isinstance(findings, list):
        return rows

    for finding in findings:

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


def validate_result(result, expected_email, expected_period):
    response_email = str(
        result.get("adminEmail") or ""
    ).strip().lower()

    response_period = str(
        result.get("period") or ""
    ).strip()

    return (
        response_email == expected_email
        and response_period == expected_period
    )


# ============================================================
# HLAVNÍ ROZHRANÍ
# ============================================================

st.title("Kontrola knih jízd")

st.caption(
    "Seyfor · Interní demonstrační aplikace"
)

st.info(
    "Vyber správce a období. Kontrola zpracuje "
    "knihy přiřazené vybranému správci. "
    "Výběr správce není ověření identity uživatele."
)

managers = load_managers()
endpoint = load_endpoint()

tab_check, tab_admin = st.tabs([
    "🚙 Kontrola knih jízd",
    "👥 Správa správců",
])


# ============================================================
# ZÁLOŽKA 1 – KONTROLA KNIH JÍZD
# ============================================================

with tab_check:

    st.subheader("Spustit kontrolu")

    if not endpoint:
        st.error(
            "Chybí konfigurace POWER_AUTOMATE_URL "
            "v Secrets aplikace."
        )

    if not managers:
        st.info(
            "Nejprve přidej správce v záložce "
            "Správa správců."
        )

    now = date.today()

    years = list(
        range(now.year - 3, now.year + 2)
    )

    if managers and endpoint:

        with st.form("logbook_check"):

            col1, col2, col3 = st.columns(
                [2.1, 1, 1]
            )

            with col1:

                selected = st.selectbox(
                    "Správce knih jízd",
                    list(managers),
                    format_func=lambda name: (
                        f"{name} — {managers[name]}"
                    ),
                )

            with col2:

                month = st.selectbox(
                    "Měsíc",
                    range(1, 13),
                    index=now.month - 1,
                    format_func=lambda m: MONTH_NAMES[m - 1],
                )

            with col3:

                year = st.selectbox(
                    "Rok",
                    years,
                    index=years.index(now.year),
                )

            submitted = st.form_submit_button(
                "Spustit kontrolu",
                type="primary",
                use_container_width=True,
            )

        if submitted:

            period = f"{year}-{month:02d}"

            expected_email = managers[selected]

            body = {
                "period": period,
                "adminEmail": expected_email,
            }

            st.session_state.pop(
                "latest_result",
                None,
            )

            st.session_state.pop(
                "latest_manager",
                None,
            )

            status_placeholder = st.empty()

            try:

                with st.spinner(
                    "Probíhá kontrola knih jízd…"
                ):

                    result = run_flow_async(
                        endpoint=endpoint,
                        body=body,
                        status_placeholder=status_placeholder,
                    )

                if not validate_result(
                    result,
                    expected_email,
                    period,
                ):

                    st.error(
                        "Finální odpověď neodpovídá "
                        "požadovanému správci nebo období. "
                        "Výsledky nebyly zobrazeny."
                    )

                    st.warning(
                        "Zkontroluj, zda Power Automate "
                        "vrací finální JSON přímo, "
                        "nebo jej balí do dalšího objektu."
                    )

                    st.write(
                        "Dostupné klíče odpovědi:",
                        list(result.keys()),
                    )

                else:

                    st.session_state[
                        "latest_result"
                    ] = result

                    st.session_state[
                        "latest_manager"
                    ] = selected

                    st.success(
                        "Kontrola byla úspěšně dokončena."
                    )

            except requests.exceptions.Timeout:

                st.error(
                    "Vypršel časový limit HTTP požadavku. "
                    "Flow může stále pokračovat. "
                    "Před opakováním zkontroluj "
                    "historii spuštění Power Automate."
                )

            except requests.exceptions.RequestException as exc:

                st.error(
                    "Komunikace s Power Automate selhala "
                    f"({type(exc).__name__}). "
                    "Zkontroluj historii běhu flow."
                )

            except TimeoutError as exc:

                st.error(str(exc))

            except ValueError as exc:

                st.error(str(exc))

    # ========================================================
    # VÝSLEDKY KONTROLY
    # ========================================================

    result = st.session_state.get(
        "latest_result"
    )

    if result:

        st.divider()

        st.subheader(
            "Výsledky poslední kontroly"
        )

        st.caption(
            f"Správce: "
            f"{st.session_state.get('latest_manager', '—')} "
            f"| Období: {result.get('period', '—')} "
            f"| RunID: {result.get('runId', '—')}"
        )

        col_a, col_b, col_c = st.columns(3)

        with col_a:

            st.metric(
                "Zkontrolované knihy",
                result.get("filesChecked", "—"),
            )

        with col_b:

            st.metric(
                "Chyby",
                result.get("errorsCount", "—"),
            )

        with col_c:

            st.metric(
                "Varování",
                result.get("warningsCount", "—"),
            )

        findings = result.get(
            "findings",
            [],
        )

        if not isinstance(findings, list):

            st.error(
                "Neplatný formát nálezů v odpovědi."
            )

        elif not findings:

            st.success(
                "Kontrola nevrátila žádné nálezy."
            )

        else:

            rows = normalize_findings(result)

            filenames = sorted(
                set(
                    str(row["Soubor"])
                    for row in rows
                )
            )

            for filename in filenames:

                file_rows = [
                    row
                    for row in rows
                    if str(row["Soubor"]) == filename
                ]

                with st.expander(
                    f"{filename} · {len(file_rows)} nálezů",
                    expanded=True,
                ):

                    st.dataframe(
                        pd.DataFrame(file_rows),
                        hide_index=True,
                        use_container_width=True,
                    )

            csv_data = (
                pd.DataFrame(rows)
                .to_csv(index=False)
                .encode("utf-8-sig")
            )

            st.download_button(
                "Stáhnout nálezy jako CSV",
                data=csv_data,
                file_name=(
                    f"nalez_"
                    f"{result.get('period', 'kontrola')}.csv"
                ),
                mime="text/csv",
            )

        json_data = json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

        st.download_button(
            "Stáhnout JSON odpověď",
            data=json_data,
            file_name=(
                f"kontrola_"
                f"{result.get('period', 'vysledek')}.json"
            ),
            mime="application/json",
        )


# ============================================================
# ZÁLOŽKA 2 – SPRÁVA SPRÁVCŮ
# ============================================================

with tab_admin:

    st.subheader("Správa správců")

    st.caption(
        "Seznam správců je uložen v souboru "
        "spravci.json, nikoli v Secrets."
    )

    # --------------------------------------------------------
    # SEZNAM SPRÁVCŮ
    # --------------------------------------------------------

    if managers:

        manager_rows = [
            {
                "Správce": name,
                "Pracovní e-mail": email,
            }
            for name, email in sorted(managers.items())
        ]

        st.dataframe(
            pd.DataFrame(manager_rows),
            hide_index=True,
            use_container_width=True,
        )

    else:

        st.info(
            "Zatím není přidán žádný správce."
        )

    # --------------------------------------------------------
    # PŘIDÁNÍ SPRÁVCE
    # --------------------------------------------------------

    st.markdown("### Přidat správce")

    with st.form(
        "add_manager",
        clear_on_submit=True,
    ):

        new_name = st.text_input(
            "Jméno správce",
            placeholder="Jan Marschik",
        )

        new_email = st.text_input(
            "Pracovní e-mail",
            placeholder="jan.marschik@seyfor.com",
        )

        add_manager = st.form_submit_button(
            "Přidat správce",
            type="primary",
        )

    if add_manager:

        name = new_name.strip()
        email = new_email.strip().lower()

        if not name:

            st.error(
                "Vyplň jméno správce."
            )

        elif not valid_email(email):

            st.error(
                "Zadej platnou e-mailovou adresu."
            )

        elif name.casefold() in (
            existing_name.casefold()
            for existing_name in managers
        ):

            st.error(
                "Správce s tímto jménem již existuje."
            )

        elif email in managers.values():

            st.error(
                "Tento e-mail je již přiřazen "
                "jinému správci."
            )

        else:

            updated_managers = {
                **managers,
                name: email,
            }

            if save_managers(updated_managers):

                st.success(
                    f"Správce {name} byl přidán."
                )

                st.rerun()

    # --------------------------------------------------------
    # ODEBRÁNÍ SPRÁVCE
    # --------------------------------------------------------

    if managers:

        st.divider()

        st.markdown("### Odebrat správce")

        remove_name = st.selectbox(
            "Vyber správce k odebrání",
            sorted(managers),
            key="remove_manager",
        )

        st.caption(
            f"E-mail: {managers[remove_name]}"
        )

        confirm_remove = st.checkbox(
            "Potvrzuji odebrání vybraného správce"
        )

        if st.button(
            "Odebrat správce",
            disabled=not confirm_remove,
        ):

            updated_managers = dict(managers)

            del updated_managers[remove_name]

            if save_managers(updated_managers):

                st.success(
                    f"Správce {remove_name} byl odebrán."
                )

                st.rerun()

    # --------------------------------------------------------
    # BEZPEČNOSTNÍ UPOZORNĚNÍ
    # --------------------------------------------------------

    st.divider()

    st.warning(
        "Správa správců není chráněna přihlášením. "
        "Každý uživatel s přístupem k aplikaci "
        "může seznam upravovat."
    )

    st.caption(
        "Při hostingu s dočasným souborovým systémem "
        "nemusí změny přežít restart nebo nové nasazení. "
        "Souborové ukládání také není vhodné "
        "pro současné úpravy více uživateli."
    )
