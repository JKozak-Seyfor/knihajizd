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
