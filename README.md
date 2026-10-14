# Kontrola knih jízd – Streamlit

Jednoduchý interní formulář pro existující Power Automate HTTP flow.

## Konfigurace

1. Vytvořte privátní GitHub repozitář a nahrajte `app.py`, `requirements.txt`, `.gitignore`, `secrets.example.toml` a `README.md`.
2. V [Streamlit Community Cloud](https://share.streamlit.io/) zvolte Create app a repozitář, vstupní soubor `app.py`.
3. V App settings → Secrets vložte obsah ze `secrets.example.toml`, ale nahraďte URL skutečnou HTTP trigger URL **z kopie flow** a testovací správce skutečnými autorizovanými osobami.
4. V Sharing ponechte aplikaci jako **Private** a výslovně přidejte povolené testující.
5. V kopii Power Automate flow používejte `Who can trigger the flow? = Anyone` **pouze pro dočasné demo**, protože se pro volání použije podepsaná URL. Samotná aplikace neověřuje skutečnou identitu správce; správce se vybírá z povoleného seznamu.
6. Otestujte jedno spuštění a zkontrolujte přidání řádků do `Nálezy.xlsx` i odpověď JSON.

## Test na počítači

```bash
python -m pip install -r requirements.txt
mkdir -p .streamlit
# Přeneste vzor secrets.example.toml do .streamlit/secrets.toml a upravte ho.
streamlit run app.py
```

## Bezpečnostní limity

- URL Power Automate s podpisem `sig` je **tajný přístupový údaj**. Nesmí být v repozitáři, sdíleném ZIP souboru, uživatelském vstupu ani v klientském JavaScriptu.
- Výběr správce v aplikaci **neověřuje identitu** a není určen pro produkční samoobslužný přístup všech správců.
- Streamlit Community Cloud je externí služba. Před načítáním skutečných osobních/provozních dat ověřte firemní pravidla pro tuto službu; pro demo ideálně testovací či anonymizovaná data.
- Privátní repozitář sám o sobě není dostatečná kontrola přístupu; zkontrolujte soukromí nasazené aplikace a nastavení sdílení.
- Neumisťujte centrální `Nálezy.xlsx` do knihovny přístupné nepovolaným osobám.
- Některá flow běží déle než časový limit HTTP odpovědi. Při timeoutu zkontrolujte historii spuštění před opakováním, ať nevzniknou duplicitní nálezy.
- Po demo před ostrým provozem doplňte skutečnou autentizaci/autorizaci uživatelů a zabezpečte původní HTTP endpoint.
