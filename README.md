# bambu-live-mcp

Server MCP (Python, FastMCP) per configurare, slicare e mostrare in tempo reale file `.3mf`
in **Bambu Studio** su Windows. Non tocca rete, account o LAN mode: la connessione **Cloud** di
Bambu Studio resta intatta (l'invio alla stampante si fa dalla GUI come sempre).

## Struttura

| Percorso | Contenuto |
|---|---|
| `bambu_live_mcp.py` | il server MCP (stdio) + `--selftest` |
| `config.example.json` | modello di `config.json` (locale, non versionato): percorso di `bambu-studio.exe`, profili base, comportamento della GUI |
| `requirements.txt` | dipendenze (`mcp[cli]` 1.x) |
| `templates/<set>/` | profili **risolti** (machine/process/filament.json), `project_settings.reference.json` (578 chiavi), `profile_set.json` |
| `test_client.py` | client MCP di prova end-to-end |
| `work/` | cartelle temporanee della CLI (tenute solo in caso di errore, max 10) |
| `venv/` | virtual environment (`mcp` 1.x, che include `FastMCP`) |

Il server trova da solo `bambu-studio.exe` (Program Files, registro di Windows, `PATH`) e i profili utente
in `%APPDATA%\BambuStudio\user\<id>`. I preset personalizzati che derivano da un preset di sistema,
per esempio `Il mio PLA` derivato da `SUNLU PLA Matte @BBL A1`, vengono risolti automaticamente.

## Installazione

Requisiti: Windows, Bambu Studio installato, Python 3.10 o superiore.

```powershell
git clone https://github.com/andreaalcaini/bambu-live-mcp.git C:\bambu-mcp
cd C:\bambu-mcp
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy config.example.json config.json   # poi modifica i profili se serve
.\venv\Scripts\python.exe bambu_live_mcp.py --selftest --slice
```

`config.json` è facoltativo: senza, valgono i default (Bambu Lab A1 0.4 + `0.20mm Standard @BBL A1` + `SUNLU PLA Matte @BBL A1`).

## Tool esposti

- **`inspect_3mf_settings(threemf_path, filter_keyword="")`**: legge `Metadata/project_settings.config`.
  Filtro con una o più parole separate da virgola (`wall`, `seam,temp`, `speed`, `support`, `infill`, `ironing`, `plate`).
  Senza filtro restituisce tutte le chiavi. Su un `.3mf` grezzo dal CAD mostra le chiavi del template.
- **`apply_settings_and_preview(threemf_path, settings_patch, rotate_z_deg=0, run_slice=True, reload_gui=True)`**
  1. `.3mf` grezzo dal CAD (o `.stl`/`.step`): lo inizializza tramite la CLI con i profili base e lo centra sul piatto.
  2. applica `settings_patch` (qualsiasi chiave; gli scalari vanno su tutti i filamenti; `20` diventa `"20%"` dove serve; aggiorna `different_settings_to_system`).
  3. ruota gli oggetti attorno a Z (`rotate_z_deg`).
  4. slicing headless e lettura di `result.json`, `slice_info.config` e intestazione G-code: tempo stimato, grammi/metri di filamento, avvisi dello slicer, più un'**analisi geometrica degli sbalzi** (facce < 30° dall'orizzontale non appoggiate al piatto).
  5. ricarica la GUI in modo sicuro (vedi [Chiusura sicura di Bambu Studio](#chiusura-sicura-di-bambu-studio)) e riapre `<nome>.gcode.3mf` in primo piano sull'anteprima.
  Parametro facoltativo `confirm_close_unsaved` (default `false`): da usare solo dopo una conferma esplicita dell'utente.
  Prima di sovrascrivere salva un backup in `.bambu_mcp_backup\` accanto al file.
- **`apply_default_cad_preset(threemf_path, seam_position="back", infill_percent=18, bed_temp=60, nozzle_temp=215, rotate_z_deg=0)`**:
  layer 0.2/0.2, Textured PEI Plate, Arachne con 3 pareti, 5 strati sopra e 4 sotto, gyroid, supporti OFF, brim OFF, piede d'elefante 0.15, cucitura `back`/`aligned`.
- **`list_profiles(kind, keyword)`** e **`set_default_profiles(machine, process, filament)`**: per usare un'altra
  stampante o un altro filamento (es. `set_default_profiles("Bambu Lab P1S 0.4 nozzle", "0.20mm Standard @BBL X1C", "Bambu PLA Basic @BBL X1C")`).

## Come funziona l'iniezione dei profili

I preset utente di Bambu (`"inherits": ...`) non vengono risolti correttamente dalla CLI: un preset utente
non appiattito produce temperature di default e peso del filamento 0 g. Per questo il server indicizza
`resources\profiles`, `%APPDATA%\BambuStudio\system` e `%APPDATA%\BambuStudio\user\<id>`, risolve
l'ereditarietà (`inherits` e `include`) e scrive preset completi in `templates\<set>\`. Poi
`bambu-studio.exe --load-settings machine.json;process.json --load-filaments filament.json --export-3mf`
crea un progetto Bambu valido, con `project_settings.config` completo e i metadati che la GUI riconosce.
Il template si rigenera da solo quando cambi profili o quando modifichi i preset in Bambu Studio.

## Comandi di test

```powershell
cd C:\bambu-mcp
# controlla sintassi e import
.\venv\Scripts\python.exe -m py_compile bambu_live_mcp.py
# diagnostica: exe, profili, template
.\venv\Scripts\python.exe bambu_live_mcp.py --selftest
# diagnostica + slicing di un cubo di prova ruotato di 15 gradi (test\selftest_cube.gcode.3mf)
.\venv\Scripts\python.exe bambu_live_mcp.py --selftest --slice
# test end-to-end via protocollo MCP (preset CAD + apertura GUI)
.\venv\Scripts\python.exe test_client.py "C:\percorso\pezzo.3mf" --rotate 90
# come sopra, senza GUI (patch wall_loops=3 + slicing)
.\venv\Scripts\python.exe test_client.py "C:\percorso\pezzo.3mf" --no-gui
# ispettore MCP interattivo (richiede Node.js)
.\venv\Scripts\mcp.exe dev bambu_live_mcp.py
```

## Registrazione

- Claude Desktop: `%APPDATA%\Claude\claude_desktop_config.json`
  ```json
  { "mcpServers": { "bambu-live-mcp": {
      "command": "C:\\bambu-mcp\\venv\\Scripts\\python.exe",
      "args": ["C:\\bambu-mcp\\bambu_live_mcp.py"] } } }
  ```
- Claude Code (scope utente):
  `claude mcp add --scope user bambu-live-mcp -- C:\bambu-mcp\venv\Scripts\python.exe C:\bambu-mcp\bambu_live_mcp.py`

## Note

- Il risultato dello slicing è `<nome>.gcode.3mf` accanto al progetto: contiene modello, impostazioni e G-code, quindi si apre direttamente in anteprima e si invia via Cloud dalla GUI.
- `gui_open_target: "project"` in `config.json` apre invece il `.3mf` di progetto (scheda Prepara).
- Se Bambu Studio ha aperto un **altro** progetto non viene chiuso: il file si apre in una nuova istanza (`gui_when_other_project_open: "skip"` per non aprire nulla).

## Chiusura sicura di Bambu Studio

Il server tratta in modo diverso ogni istanza di Bambu Studio aperta. Per riconoscere il file aperto usa il percorso esatto in
`%TEMP%\bamboo_model\...\origin.txt` e il titolo della finestra, dove `* ` indica modifiche non salvate.

| Finestra aperta | Cosa fa il tool |
|---|---|
| Il file che il tool sovrascrive (stesso percorso) | Chiusura normale (WM_CLOSE). Se compare "Bambu Studio - Save" preme **No** con un click inviato alla finestra (nessun mouse o tastiera reale) e non spunta "Remember my choice". |
| Stesso nome, ma percorso **non verificabile** (dati di ripristino assenti), **con** `*` | Non la chiude. Risponde con `needs_confirmation: true` e un avviso. Solo dopo la conferma dell'utente si rilancia con `confirm_close_unsaved: true`. |
| Stesso nome, percorso non verificabile, **senza** modifiche | Chiusura normale (non si perde nulla). |
| Stesso nome ma **percorso diverso** (noto) | È un altro progetto: non la tocca mai. |
| Un altro progetto (con o senza `*`) | Non la tocca mai. Se ha modifiche non salvate lo segnala in `warnings`. |

Termina il processo solo se la finestra del file del tool non si chiude entro `gui_close_timeout_s`, e solo quel processo.
Subito dopo cancella le sue cartelle di ripristino, così al riavvio Bambu Studio non propone di ripristinare il progetto.
Cancella anche le cartelle di ripristino lasciate dalla CLI di slicing.

Nella risposta del tool:
- `unsaved_changes_found` dice se c'erano modifiche non salvate;
- `warnings` contiene gli avvisi da riferire all'utente;
- `gui_summary` dice in breve cosa è stato fatto a ogni finestra;
- `gui.close.instances` riporta il dettaglio di ogni istanza: `relation`, `unsaved_changes`, `action`, `recovery_data_removed`.

Il popup "New version of Bambu Studio" viene ignorato: non blocca la chiusura.
- Tieni Bambu Studio aggiornato: la CLI e i profili di sistema sono quelli installati.

## Licenza

MIT, vedi [LICENSE](LICENSE). Progetto indipendente, non affiliato a Bambu Lab.
