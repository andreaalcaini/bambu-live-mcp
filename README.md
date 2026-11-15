# bambu-live-mcp

Server MCP (Python, FastMCP) per configurare, slicare e mostrare in tempo reale file `.3mf`
in **Bambu Studio** su Windows. Non tocca rete, account o LAN mode: la connessione **Cloud** di
Bambu Studio resta intatta (l'invio alla stampante si fa dalla GUI come sempre).

## Struttura

| Percorso | Contenuto |
|---|---|
| `bambu_live_mcp.py` | il server MCP (stdio) + `--selftest` |
| `bambu_project.py` | modello del progetto `.3mf` (oggetti, mesh, piatti, trasformazioni) e geometria (impronte, disposizione, sbalzi, cucitura) |
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

Tutti i tool che modificano un progetto seguono la stessa pipeline:
1. copia di lavoro (i `.3mf`/`.stl`/`.step` grezzi dal CAD vengono prima inizializzati con i profili base);
2. modifica;
3. **controllo del layout** (sovrapposizioni tra oggetti, uscita dal piatto, altezza massima, oggetti sollevati), con **ridisposizione automatica** dei piatti con problemi;
4. export tramite la CLI di Bambu Studio, che rigenera **miniature di ogni piatto**, `filament_sequence.json` e relazioni interne, e verifica che il file sia valido;
5. slicing (stime per piatto);
6. backup in `.bambu_mcp_backup\` (ultimi `backups_to_keep` per progetto, default 10) e salvataggio.

La GUI **non** viene ricaricata di default: alla fine di più modifiche usa `reload_gui`.
Se lo slicing fallisce, `slice_failure` dice se il progetto è stato comunque salvato e qual è la causa probabile (es. sovrapposizioni).

Coordinate: le posizioni e i box sono in mm **sul piatto** dell'oggetto (origine nell'angolo del piatto), le stesse di `list_objects.bbox_on_plate` e del G-code.

### Impostazioni
- **`inspect_3mf_settings(threemf_path, filter_keyword="")`**: legge `Metadata/project_settings.config`.
  Il filtro accetta una o più parole separate da virgola (`wall`, `seam,temp`, `speed`, `support`, `infill`, `ironing`, `plate`).
  Senza filtro restituisce tutte le chiavi; su un `.3mf` grezzo mostra quelle del template.
- **`apply_settings_and_preview(threemf_path, settings_patch, rotate_z_deg=0, run_slice=True, reload_gui=False, confirm_close_unsaved=False)`**:
  - accetta qualsiasi chiave; un valore scalare vale per tutti i filamenti e `20` diventa `"20%"` dove serve;
  - `rotate_z_deg` ruota ogni oggetto attorno al proprio centro e, se poi i pezzi si sovrappongono, li ridispone.
- **`apply_default_cad_preset(threemf_path, seam_position="back", infill_percent=18, bed_temp=60, nozzle_temp=215, rotate_z_deg=0, filament_profile="", filament_colour="", reload_gui=False)`**:
  - imposta layer 0.2/0.2, Textured PEI Plate, Arachne con 3 pareti, 5 strati sopra e 4 sotto, gyroid, supporti e brim disattivati, piede d'elefante 0.15, cucitura `back`/`aligned`;
  - `filament_profile` (es. `SUNLU PLA Matte @BBL A1` o un preset utente) e `filament_colour` (es. `#000000`) valgono **solo per quel progetto**;
  - con `bed_temp=0` / `nozzle_temp=0` restano le temperature del profilo.
- **`list_profiles(kind, keyword)`** e **`set_default_profiles(machine, process, filament)`**: profili disponibili e default per i **futuri** progetti grezzi.

### Oggetti e piatti
- **`list_objects(project_3mf)`**: piatti (nome, origine) e oggetti (ingombro sul piatto, dimensioni, rotazione Z, triangoli), più `layout_check`.
- **`replace_meshes(project_3mf, cad_3mf, match="name", name_map=None, run_slice=True, reload_gui=False)`**: aggiorna la geometria da una nuova esportazione del CAD e mantiene `project_settings`, piatto, posizione XY, rotazione e impostazioni per oggetto.
  - **Abbinamento:** per nome, esatto e poi ignorando maiuscole, spazi e `_`. `name_map` (`{"Stampa_Coperchio": "Enclosure_Coperchio"}`) serve per gli abbinamenti espliciti.
  - **Allineamento:** la nuova mesh viene **registrata** su quella vecchia, così le parti invariate restano ferme, e il pezzo viene riappoggiato al piatto.
  - **Resoconto:** riporta `matched`, `new_in_cad` e `missing_in_cad`, con suggerimenti.
- **`merge_projects(target_3mf, source_3mf, as_new_plate=True, plate_name="", target_plate=1, ...)`**: copia gli oggetti di un altro progetto o di un file CAD grezzo, con le loro impostazioni per oggetto.
  - Con `as_new_plate=True` ogni piatto della sorgente diventa un nuovo piatto.
  - ID, UUID, origini dei piatti, `filament_sequence.json` e miniature li calcola il tool.
- **`add_plate(project_3mf, name)`** e **`assign_to_plate(project_3mf, object_name, plate, plate_name="", arrange=True)`**:
  - quando cambia il numero di piatti, il tool ricalcola la griglia come Bambu Studio (passo = piatto × 1.2, righe verso −Y) e sposta gli oggetti;
  - con `plate` = ultimo piatto + 1, il piatto viene creato.
- **`transform_object(project_3mf, object_name, rotate_x=0, rotate_y=0, rotate_z=0)`**: ruota l'oggetto attorno al suo centro (assi del piatto, ordine X, Y, Z) e lo riappoggia al piatto. Con `rotate_x=180` capovolge un coperchio.
- **`lay_on_face(project_3mf, object_name, face="largest_flat" | [x, y, z])`**: appoggia sul piatto la faccia piana esterna più grande, oppure quella con la normale indicata.
- **`arrange(project_3mf, plate=0, spacing_mm=6)`**: dispone gli oggetti a ripiani, senza sovrapposizioni e centrati (`plate=0` = tutti i piatti). Non ruota i pezzi.

### Sbalzi e cucitura
- **Analisi sbalzi** (`overhang_analysis` in ogni risposta con slicing): per piatto e oggetto, regioni `{z, area_mm2, tipo, xy_bbox, span_mm}`.
  - **Tipi di regione:**
    - `ponte`: soffitto orizzontale appoggiato su due lati opposti; `span_mm` è la luce libera.
    - `sbalzo`: soffitto senza appoggi opposti.
    - `inclinata`: faccia verso il basso oltre la soglia di inclinazione.
  - **Verdetto:**
    - `ok`: i ponti fino a `bridge_ok_span_mm` (default 15) non sono considerati un problema;
    - `da_verificare`: ponti fino al doppio della soglia, o superfici molto inclinate;
    - `attenzione`: sbalzi senza appoggio, o ponti più lunghi.
- **`seam_report(gcode_3mf, band_mm=5, avoid_box=None, avoid_plate=0)`**: legge dal G-code l'inizio di ogni giro di parete esterna.
  - **Raggruppamento:** per piatto, oggetto e fascia di altezza riporta le posizioni reali più frequenti (cluster), ciascuna con conteggio e lato dell'oggetto.
  - **Zona da evitare:** con `avoid_box` `[xmin, ymin, zmin, xmax, ymax, zmax]` conta le cuciture che ci cadono dentro (`avoid_box_ok`).
- **`paint_seam(project_3mf, object_name, box, mode="block" | "enforce" | "clear")`**: vernicia la cucitura sui triangoli che intersecano il box, come la verniciatura della cucitura di Bambu Studio.
  - **Precisione:** le mesh CAD hanno triangoli grandi, quindi l'area marcata può superare il box.
  - **Limite:** se il contorno di un elemento piccolo sta tutto nel box, Bambu Studio deve comunque mettere la cucitura su quel giro.
  - **Verifica:** dopo la verniciatura, controlla con `seam_report`.

### GUI
- **`reload_gui(project_3mf, confirm_close_unsaved=False)`**: ricarica Bambu Studio una volta sola, con le regole di chiusura sicura. Riapre il `.gcode.3mf` se è aggiornato, altrimenti il progetto.

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
