# blender-build — pattern appresi durante le run (knowledge base versionato)

Principi e gotcha per la modellazione 3D procedurale in Blender via MCP `execute_blender_code`,
**accumulati run dopo run**. Questo file è **versionato** (vive in `run/`, non nell'auto-memory
fuori repo) ed è **scrivibile dal contesto RUN** (non è sotto `run/.claude/skills/`, che è
read-only): quando durante una run emerge un nuovo problema/soluzione, **aggiungilo qui** e
committa, così la conoscenza resta nel repo e condivisa tra macchine/contesti.

Valgono per qualunque modellazione procedurale in Blender guidata da `execute_blender_code`.

## Processo
- **Costruisci incrementale e verifica a schermo a ogni step rischioso.** I parametri estetici (angolo twist, densità fori, spessore) vanno *iterati guardando screenshot*, non indovinati: es. twist 130° distrugge un pattern, 40° lo esalta.
- **Snapshot prima di operazioni rischiose**; per annullare NON usare `bpy.ops.ed.undo` (può crashare la connessione MCP) → **ricarica l'ultimo `.blend`** buono.
- **Diagnostica = sempre più viste.** Una sola vista nasconde difetti: cattura FRONT + RIGHT + 3/4. Imposta la vista con `bpy.context.temp_override(area, region)` + `view3d.view_axis` (+ `view_orbit` per la 3/4) + `view_selected`.

## Boolean robusti (regola d'oro: operandi PULITI e NON auto-intersecanti)
- **Un cutter/operando che si auto-interseca rende EXACT degenere e può SVUOTARE il risultato.** Cause tipiche: tanti prismi sovrapposti in un unico mesh; prismi **radiali** che, puntando verso l'asse di un cilindro, **convergono e si sovrappongono nella zona interna** (succede solo con fori grandi).
  - Fix: tieni gli elementi del cutter **non sovrapposti**; oppure **un boolean per gruppo non-sovrapposto in sequenza**; oppure fai prima un **self-union** del cutter.
- **Per forare vicino a un bordo aperto**: confina i tagli in una fascia lontana dal rim, altrimenti il boolean lascia schegge/caos sul bordo.
- **`BOOLEAN` preserva i materiali degli operandi solo con `modifier.material_mode='TRANSFER'`** (il default `'INDEX'` rimappa tutto allo slot 0 → un colore solo). Quindi: assegna i materiali alle parti **prima** di unirle.
- **Parti "connesse" davvero**: non appoggiare, far **compenetrare** e poi `BOOLEAN UNION` in un solido unico (controlla "componenti connesse" = 1).

## Quando NON usare il boolean
- **Reticolo a fili / pattern fitto e uniforme**: invece di forare, costruisci la **maglia di facce** del pattern e applica il **WIREFRAME modifier** (`thickness`=spessore filo, `use_boundary=True`) → fili uniformi e sottili a qualsiasi scala, niente convergenza/auto-intersezione. Generalizzazione: *modellare il positivo (la trama) è spesso più robusto che sottrarre il negativo (i fori)*, soprattutto su superfici curve.

## Geometria procedurale
- **Disporre N elementi in cerchio**: il modifier ARRAY con Object-Offset (empty ruotato) è fragile (copie sparse) → usa un **loop Python su theta** + `join`. Più in generale: per layout regolari, un loop esplicito è più prevedibile dei modifier di replica.
- **Tassellazioni corrette, non "a occhio"**: per un honeycomb che tassella davvero usa le formule (pointy-top: passo colonna `√3·r_cell`, riga `1.5·r_cell`, righe alterne sfalsate di mezzo passo; `r_cell=(2πR/N)/√3` su cilindro di raggio R con N colonne). Parametri arbitrari producono overlap o buchi.
- **Allineare elementi a un pattern**: posizionali agli stessi angoli/passi del pattern (es. barre verticali agli angoli delle colonne del reticolo) e centra sullo stesso raggio.

## Modifier: gotcha di centro/asse
- I deform (**Simple Deform TWIST/BEND**) ruotano attorno all'**origine dell'oggetto**, non al centro geometrico. Dopo un `join` l'origine sta sull'oggetto attivo (spesso decentrata) → il twist *splaya* a ventaglio.
  - Fix: assegna al modifier un **Origin = empty nel punto/asse voluto** (`mod.origin = empty`); NON spostare `location` (muoverebbe la mesh).
  - L'`angle` si distribuisce sul **bounding-box locale** lungo l'asse → per deformare più parti in modo coerente, **uniscile prima** e deforma l'insieme (estensione comune).
- **Maniglia / arco pulito: usa SCREW, non SIMPLE_DEFORM BEND.** Per una cinghia ad arco (manico) il BEND con origin=empty lontano dall'oggetto produce facilmente blob/twist storti (visto sulla radio: 3 tentativi falliti). Robusto: crea il **profilo della sezione** (rettangolo cinghia, es. 16×5 mm) a distanza R dall'asse, applica **SCREW** `angle=180°`, `axis` nel piano voluto (es. 'X' per arco fronte-retro nel piano Y-Z), `steps≈48-64`, `screw_offset=0` → mezzo anello/arco perfetto e simmetrico, nessuna torsione. La sezione "extrude" della Bezier non si orienta facilmente nel piano Y-Z (esce sottile) → SCREW è più affidabile.

## Scala e numeri
- Modella in **mm tondi e proporzionali** (1 BU = 1 mm: `unit_settings.scale_length=0.001`, `length_unit='MILLIMETERS'`). Evita frazioni di mm arbitrarie (no 0.0035/0.0385); usa 2, 6, 38, 55. Frazioni solo se necessarie.

## Screenshot / resa
- **Vista pulita** (forma): `shading.type='SOLID'`, `light='MATCAP'/'STUDIO'`, `show_cavity=True`, `background_type='VIEWPORT'` chiaro, overlay/gizmo/pannelli OFF.
- **Vedere la topologia**: SOLID + `overlay.show_wireframes=True` (`wireframe_threshold=1.0`).
- **Material Preview**: lo studiolight è spesso sovraesposto e brucia la geometria sottile → riduci `studiolight_intensity` (~0.45) per giudicare i colori veri.
- **Render del GATE per il giudice: NON usare SOLID grigio piatto.** Un render `SOLID` uniforme grigio (anche con `color_type='MATERIAL'`, perché il base_color del Principled BSDF NON è il colore viewport) appiattisce il contrasto: il giudice legge come "assenti" feature che ci sono (staffe angolari, vent strip, finestra dial, fori) → punteggio ingiustamente basso e raccomandazione `reiterate` spuria. Fix: per i render del gate imposta i **viewport display color** delle parti (`obj.active_material.diffuse_color` o `color_type='OBJECT'` con `obj.color` distinti per gruppo) **oppure** usa shading `MATERIAL`/`RENDERED`, così i materiali matte distinti rendono leggibili le feature. In alternativa attiva `overlay.show_wireframes`/`show_cavity` per dare rilievo agli spigoli. Lo screenshot pulito (overlay/gizmo OFF, `render.opengl(view_context=True)` + `save_render`) resta giusto: è solo il *grigio piatto* il problema.

## Estetica
- **Premium vs "giocattolo"**: niente metallic lucido né colori saturi; palette **matte** (`metallic=0`, roughness ~0.7, `Specular IOR Level`~0.25), pochi toni distinti (scuro/medio/chiaro). Forme: meno è meglio; trame regolari e pulite battono trame fitte e "spinose".

## Orientamento poligoni regolari
- **Vertice vs faccia davanti**: prima di scavare una nicchia/targhetta su una faccia di un prisma poligonale, verifica se al fronte c'è una FACCIA o uno SPIGOLO. `primitive_cylinder(vertices=6)` ha un vertice a +X (0°); ruotando di π/6 il vertice resta/torna a fronte → una nicchia centrata sul fronte taglia DUE facce (sembra due fessure). Per una faccia piatta frontale: rotazione 0 (o multipli di 60°). In generale: ruota il poligono così che l'asse d'interesse cada al centro di una faccia, non su uno spigolo.
- **Elemento "amalgamato" (non appiccicato)**: per integrarlo nella base (stile targhetta Oscar) modella la base come piedistallo solido abbastanza alto e SCAVA l'elemento nel suo volume (recess/nicchia incorniciata dal materiale base), invece di unire un blocco sporgente. Tienilo entro il volume solido (sotto la parte cava) così non interseca il resto.

## Manifold / stampabilità
- **Watertight vs Blender-manifold**: Blender può riportare 0 edge non-manifold mentre l'STL risulta NON watertight (trimesh) → spesso da **facce complanari coincidenti** (es. fondo del reticolo esattamente sul piano superiore della base). Fix: far **compenetrare** le parti di ~2 mm (mai piani coincidenti); verifica SEMPRE l'STL con trimesh (`is_watertight`, `is_winding_consistent`, `body_count`, bbox).
- **Export STL print-ready**: `bpy.ops.wm.stl_export(..., use_scene_unit=False, global_scale=1.0)`. Modellando con valori-mm grezzi (1 BU = 1 mm) lo slicer (che assume mm) ottiene la scala giusta; `use_scene_unit=True` con `scale_length=0.001` rimpicciolirebbe 1000x.
- **Autoportanza**: evita ponti orizzontali larghi (es. cielo piatto di una cavità) → cavità **coniche/inclinate** (≤45° overhang). Honeycomb **pointy-top** è quasi autoportante (montanti ~60°, niente tratti orizzontali in cima).
- **Fori in parete verticale autoportanti**: il "soffitto" del foro non deve essere un lato PIATTO orizzontale (= ponte → supporti) ma deve convergere a **VERTICE in alto** (esagono pointy-top / teardrop) → arco ~60° autoportante. Attenzione: `bmesh.ops.create_cone(segments=6)` può mettere un FLAT in cima nella tua orientazione → aggiungi un **roll di π/6 attorno all'asse** del prisma per portare un vertice in alto. NB orientamento di stampa: se il pezzo si stampa capovolto, "in alto" è invertito → un esagono pointy-top E pointy-bottom è autoportante in entrambi i versi.
- **Cutter prismatici: usa `create_cone`, non prismi a mano**: costruire prismi via bmesh manuale (verts/faces a mano) rischia normali/winding incoerenti → mesh non valida → boolean rovina il pezzo. `bmesh.ops.create_cone(cap_ends=True, segments=N, matrix=...)` dà prismi puliti e manifold.
- Pulizia in EDIT mode: `remove_doubles(0.05)` + `normals_make_consistent` + `select_non_manifold` → `fill_holes`. Verifica contando gli edge non-manifold (`e.is_manifold`) prima/dopo. Il reticolo Wireframe arriva facilmente a 0 (watertight).
- **Pochi edge non-manifold (>2 facce) alle giunzioni** (parete/pilastro/foro che si incontrano): non sono facce duplicate (`unique_faces` non li toglie) → restano dopo i fix. **Gli slicer (Bambu Studio/OrcaSlicer/Cura/PrusaSlicer) li riparano da soli all'import**: con 1 corpo + winding coerente + 0 buchi il pezzo è stampabile. Il `voxel remesh` garantisce watertight ma ARROTONDA gli spigoli vivi → evitarlo se servono sharp.

## Ridurre tempo di stampa (alleggerimento)
- **Trova dove sta il volume**: spesso una parte piena domina (qui il piedistallo era ~85% del volume). Alleggerisci QUELLA.
- **Openwork visibile > svuotamento interno nascosto**: forare/reticolare la massa (stesso linguaggio honeycomb del resto) alleggerisce *ed* è bello e coerente; lo svuotamento interno (guscio) alleggerisce ma non si vede. Risultato qui: volume da ~145 a ~59 cm³ (~60% in meno).
- Modella in **valori-mm grezzi** così le dimensioni reali del print sono quelle dei numeri.

## Connessione senza intersezione
- Per far **poggiare** una parte sopra un'altra senza interpenetrarla (richiesta "non si intersechi, poggia al max"): usa **pilastri passanti** (es. le stanghe full-height z0→top) che attraversano entrambe e le fondono, mentre la parte superiore appoggia il suo bordo sul top dell'altra. Così sono un solido unico anche con intersezione ~nulla.

## Processo (design iterativo)
- L'estetica si converge **iterando a schermo**: build → screenshot (3 viste) → l'utente reagisce → aggiusta UN parametro per volta. Aspettarsi molte iterazioni; salvare snapshot/STL a ogni step (storico) e ri-verificare l'STL con trimesh dopo ogni modifica destinata alla stampa.
