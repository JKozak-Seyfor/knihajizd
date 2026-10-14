import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import requests
import streamlit as st


# ============================================================
# KONFIGURACE
# ============================================================

st.set_page_config(
    page_title="Kontrola knih jízd | Seyfor",
    page_icon="🚙",
    layout="wide"
)

MANAGERS_FILE = Path(__file__).resolve().parent / "spravci.json"

MONTH_NAMES = [
    "Leden", "Únor", "Březen", "Duben",
    "Květen", "Červen", "Červenec", "Srpen",
    "Září", "Říjen", "Listopad", "Prosinec"
]


# ============================================================
# SPRÁVA SPRÁVCŮ
# ============================================================

def load_managers():
    """Načte seznam správců ze souboru spravci.json."""

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
    """Bezpečně uloží seznam správců do JSON souboru."""

    temp_name = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=MANAGERS_FILE.parent,
            prefix=".spravci-",
            suffix=".tmp",
            delete=False
        ) as handle:

            temp_name = handle.name

            json.dump(
                managers,
                handle,
                ensure_ascii=False,
                indent=2
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
    """Ověří základní syntaxi e-mailové adresy."""

    return bool(
        re.fullmatch(
            r"[^\s@]+@[^\s@]+\.[^\s@]+",
            email
        )
    )


# ============================================================
# POWER AUTOMATE
# ============================================================

def load_endpoint():
    """Načte URL Power Automate ze Streamlit Secrets."""

    try:
        return str(st.secrets["POWER_AUTOMATE_URL"])
    except Exception:
        return ""


# ============================================================
# ZPRACOVÁNÍ VÝSLEDKŮ
# ============================================================

def normalize_findings(payload):
    """Převede nálezy z JSON odpovědi do tabulkové podoby."""

    rows = []

    for finding in payload.get("findings", []):

        if not isinstance(finding, dict):
            continue

        rows.append({
            "Soubor": finding.get("file", ""),
            "Závažnost": finding.get("severity", ""),
            "Pravidlo": finding.get("rule", ""),
            "Řádek": finding.get("row", ""),
            "Popis": finding.get("message", "")
        })

    return rows


# ============================================================
# HLAVNÍ ROZHRANÍ
# ============================================================

st.title("Kontrola knih jízd")

st.caption(
    "Seyfor · Interní demonstrační aplikace"
)

st.info(
    "Vyber správce a období. Kontrola zpracuje knihy "
    "přiřazené vybranému správci. "
    "Výběr správce není ověření identity uživatele."
)

managers = load_managers()
endpoint = load_endpoint()

tab_check, tab_admin = st.tabs([
    "🚙 Kontrola knih jízd",
    "👥 Správa správců"
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
                    )
                )

            with col2:

                month = st.selectbox(
                    "Měsíc",
                    range(1, 13),
                    index=now.month - 1,
                    format_func=lambda m: MONTH_NAMES[m - 1]
                )

            with col3:

                year = st.selectbox(
                    "Rok",
                    years,
                    index=years.index(now.year)
                )

            submitted = st.form_submit_button(
                "Spustit kontrolu",
                type="primary",
                use_container_width=True
            )

        # ----------------------------------------------------
        # SPUŠTĚNÍ POWER AUTOMATE
        # ----------------------------------------------------

        if submitted:

            period = f"{year}-{month:02d}"

            body = {
                "period": period,
                "adminEmail": managers[selected]
            }

            # Odstranění výsledků předchozí kontroly
            st.session_state.pop("latest_result", None)
            st.session_state.pop("latest_manager", None)

            try:

                with st.spinner(
                    "Probíhá kontrola knih jízd. "
                    "Zpracování může trvat několik minut…"
                ):

                    response = requests.post(
                        endpoint,
                        json=body,
                        timeout=(15, 240)
                    )

                    response.raise_for_status()

                    result = response.json()

                if not isinstance(result, dict):
                    raise ValueError(
                        "Odpověď není JSON objekt."
                    )

                response_email = str(
                    result.get("adminEmail") or ""
                ).strip().lower()

                response_period = result.get("period")

                if (
                    response_email != managers[selected]
                    or response_period != period
                ):

                    st.error(
                        "Odpověď neodpovídá požadovanému "
                        "správci nebo období. "
                        "Výsledky nebyly zobrazeny."
                    )

                else:

                    st.session_state["latest_result"] = result
                    st.session_state["latest_manager"] = selected

                    st.success(
                        "Kontrola byla dokončena."
                    )

            except requests.exceptions.Timeout:

                st.error(
                    "Časový limit požadavku vypršel. "
                    "Flow může stále pokračovat. "
                    "Před opakováním zkontroluj historii "
                    "spuštění Power Automate."
                )

            except requests.exceptions.RequestException as exc:

                st.error(
                    "Volání Power Automate selhalo "
                    f"({type(exc).__name__}). "
                    "Zkontroluj historii spuštění flow "
                    "a nastavení endpointu."
                )

            except (ValueError, json.JSONDecodeError):

                st.error(
                    "Flow nevrátilo očekávanou JSON odpověď."
                )

    # --------------------------------------------------------
    # ZOBRAZENÍ VÝSLEDKŮ
    # --------------------------------------------------------

    result = st.session_state.get("latest_result")

    if result:

        st.divider()

        st.subheader("Výsledky poslední kontroly")

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
                result.get("filesChecked", "—")
            )

        with col_b:
            st.metric(
                "Chyby",
                result.get("errorsCount", "—")
            )

        with col_c:
            st.metric(
                "Varování",
                result.get("warningsCount", "—")
            )

        findings = result.get("findings", [])

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
                    row for row in rows
                    if str(row["Soubor"]) == filename
                ]

                with st.expander(
                    f"{filename} · {len(file_rows)} nálezů",
                    expanded=True
                ):

                    st.dataframe(
                        pd.DataFrame(file_rows),
                        hide_index=True,
                        use_container_width=True
                    )

            # Export CSV
            csv_data = pd.DataFrame(rows).to_csv(
                index=False
            ).encode("utf-8-sig")

            st.download_button(
                "Stáhnout nálezy jako CSV",
                data=csv_data,
                file_name=(
                    f"nalez_{result.get('period', 'kontrola')}.csv"
                ),
                mime="text/csv"
            )

        # Export JSON
        json_data = json.dumps(
            result,
            ensure_ascii=False,
            indent=2
        ).encode("utf-8")

        st.download_button(
            "Stáhnout JSON odpověď",
            data=json_data,
            file_name=(
                f"kontrola_{result.get('period', 'vysledek')}.json"
            ),
            mime="application/json"
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
                "Pracovní e-mail": email
            }
            for name, email in sorted(managers.items())
        ]

        st.dataframe(
            pd.DataFrame(manager_rows),
            hide_index=True,
            use_container_width=True
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
        clear_on_submit=True
    ):

        new_name = st.text_input(
            "Jméno správce",
            placeholder="Jan Marschik"
        )

        new_email = st.text_input(
            "Pracovní e-mail",
            placeholder="jan.marschik@seyfor.com"
        )

        add_manager = st.form_submit_button(
            "Přidat správce",
            type="primary"
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
                "Správce s tímto jmén
