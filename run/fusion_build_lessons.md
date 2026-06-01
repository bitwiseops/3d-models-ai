# fusion-build — pattern per la modellazione parametrica in Fusion 360 via MCP (knowledge base versionato)

Principi e gotcha per la modellazione 3D **parametrica / B-rep** in Fusion 360 via MCP
(esecuzione di codice `adsk.*` sulla coda main-thread dell'add-in), **da accumulare run dopo
run**. Questo file è **versionato** (vive in `run/`, non nell'auto-memory fuori repo) ed è
**scrivibile dal contesto RUN** (non è sotto `run/.claude/skills/`, che è read-only): quando
durante una run emerge un nuovo problema/soluzione, **aggiungilo qui** e committa.

> ⚠️ **Seed iniziale + prima validazione reale (2026-05-31).** Il progetto è stato scritto su
> Linux (dove Fusion non gira); la **prima run end-to-end sul Mac** è avvenuta il 2026-05-31 — vedi
> la sezione **"✅ Validato su Fusion reale"** in fondo per i gotcha confermati/nuovi. Le voci
> ancora marcate `[da confermare]` non sono state verificate in prima persona; togli il marcatore
> quando le vedi funzionare. Setup MCP: sezione `fusion-build` in `run/README.md`.

Valgono per qualunque ricostruzione procedurale parametrica in Fusion guidata da MCP.

## Vincoli HARD del backend (non negoziabili)
- **API non thread-safe → coda main-thread.** Ogni operazione gira sul main thread dell'add-in
  (gestito dal server). Non tentare concorrenza.
- **Una operazione logica per chiamata, attesa.** Niente operazioni sovrapposte/batch in una sola
  chiamata: fa crashare l'add-in. Gli executor sono **rigorosamente in sequenza**.
- **Budget ~30s per chiamata.** Spezza gli step pesanti (timeline lunga, molte feature) in script
  più piccoli e fai snapshot `.f3d` tra l'uno e l'altro. Niente loop enormi in un singolo script.
- **🛑 MAI loop non limitati (`while True`) che mutano la geometria.** Ogni `deleteMe()`/feature
  rigenera il modello: un `while True: body.deleteMe()` su molti body **appende l'app** (CPU 100%,
  l'MCP va in timeout, serve force-quit di Fusion). Visto in run 2026-05-31 iter3: l'add-in si e
  bloccato e ha richiesto riavvio manuale. Regola: per cancellare in blocco, **snapshot dei nomi
  in una lista e poi un solo passaggio** (`for b in [x for x in root.bRepBodies if cond]: b.deleteMe()`),
  mai un loop che ricontrolla `bRepBodies` dopo ogni delete. Metti sempre un **cap esplicito** ai
  loop e preferisci operazioni batch.
- **Unità interne = centimetri.** 1 mm = 0.1 cm. Scrivi le quote in cm, tonde e proporzionali; il
  gate converte la bbox cm→mm (×10).
- **Annullare = riaprire l'ultimo `.f3d` buono** o riportare indietro il marker della timeline e
  cancellare le feature successive. NON affidarti a comandi di undo programmatici.

## Esattezza per costruzione (il senso del backend parametrico)
- **Sketch vincolato e quotato → extrude/revolve.** La silhouette giusta nasce dai **vincoli**
  dello sketch (geometrici + dimensionali), non da aggiustamenti a valle. Punta a sketch
  *fully-constrained* (nessun grado di libertà residuo) per geometria ripetibile.
- **Solidi di rivoluzione = Revolve, non mesh.** Corpo teiera, bottiglia, coperchio, manopole:
  disegna la **metà-sezione** del profilo su un piano che contiene l'asse e fai **Revolve 360°**
  attorno all'asse. Esatto e simmetrico per costruzione.
- **Prismi/estrusi = Extrude** di un profilo chiuso. Per spessori sottili o gusci: **Shell**.
- **Transizioni di sezione = Loft/Sweep** (es. beccuccio che cambia profilo lungo un percorso).

## Simmetria
- **Mirror feature sul piano di simmetria** è il default per gli oggetti simmetrici (analogo del
  MIRROR di Blender): modella metà, specchia → simmetria perfetta e parametrica, nessun
  disallineamento. Usa i piani base (XY/XZ/YZ) o un piano-feature costruito.
- Per ripetizioni regolari (manici multipli, fori in cerchio): **Circular/Rectangular Pattern**,
  non copie posizionate a mano.

## Assieme e posizionamento — JOINTS, non trasformazioni [da confermare in dettaglio sul Mac]
- **Ogni parte = un Component proprio**, creato all'origine del suo sketch; poi posizionato
  **relativamente** al target con un **Joint**. È il punto della migrazione: l'intento relativo
  sopravvive alle modifiche parametriche.
- **VIETATO posizionare con `occurrence.transform2` (matrice mondo assoluta).** Bakera la
  posizione e annulla il vantaggio parametrico. Trasformazione transitoria solo come fallback
  ultimo e documentato.
- **Riferimenti stabili = JointOrigin** su facce/spigoli/punti nominati (o piani-feature), non
  indici di facce volatili che cambiano alla rigenerazione.
- Mappa relazioni del manifest → joint:
  - `concentric_with`/coaxial → **Cylindrical**/**Rigid** su spigoli/assi concentrici.
  - `on_top_of`/`flush_with` → **Planar** tra le facce a contatto (offset 0 = a filo).
  - `attached_to` → **Rigid** con JointOrigin coincidenti.
  - `inside` → **concentric** + **planar** con offset per sedere dentro.
  - `symmetric_to` → **Mirror feature** sul piano di simmetria.
- **Gotcha atteso [da confermare]:** scegliere la *geometria* di joint giusta da geometria appena
  creata è la parte fragile. Crea JointOrigin espliciti **subito dopo** aver creato la faccia/asse
  di interesse, mentre il riferimento è "fresco"; non rimandare a dopo altre feature.

## Dettagli = feature native, non editing mesh
- **Raccordi = Fillet**, **smussi = Chamfer**, **fori = Hole** (con tipo/diametro nativi),
  **scanalature = revolve-cut** o extrude-cut di un profilo. Niente boolean su mesh.
- Applica i dettagli **dopo** che la forma base e i joints sono a posto (step finali del piano).

## Render di verifica (pose ESATTE, apples-to-apples col gate)
- Convenzione di build: **up = +Z, "fronte" verso −Y.** Le 4 pose canoniche (`VIEW_CAMERA` in
  `src/fusion_build.py`): front=sguardo +Y, side=−X, back=−Y (ortografiche), three_quarter=iso.
- Imposta `app.activeViewport.camera` (`eye`/`target`/`upVector`, `cameraType`),
  `camera.isFitView=True` o `viewport.fit()`, **riassegna** `viewport.camera = camera` (in Fusion
  la camera va riassegnata perché abbia effetto [da confermare]), poi
  `viewport.saveAsImageFile(path, w, h)`.
- **Usa le STESSE pose negli step e nel gate**, così i render combaciano coi `refs/`.
- [da confermare] Per render più leggibili (feature visibili al giudice) valuta visual style
  "shaded with edges" e isolamento overlay; evita un grigio piatto che appiattisce il contrasto
  (stesso rischio visto in blender-build).

## Metriche del gate
- **bbox**: da `body.boundingBox`/bbox del component, in **cm** → ×10 = mm, confronta con
  `target_mm` → `proportion_error_pct`.
- **watertight**: i solidi B-rep sono chiusi per costruzione (`body.isSolid`); per parità di
  calcolo col gate di blender-build, esporta STL e verifica con trimesh
  (`is_watertight`/`is_winding_consistent`).
- **simmetria**: misura lo scostamento rispetto al piano di simmetria (bbox simmetrica o confronto
  specchiato) in mm per asse.

## Export
- Snapshot per modifica: salva il design come **`.f3d`** (Fusion Archive) via export manager.
- Finali: **STEP** (`final.step`) per CAD a valle e **STL** (`final.stl`) per mesh/stampa.
- I path passati a Fusion sono **assoluti** (stessa macchina di Claude Code).

## Processo (design iterativo)
- Build incrementale coarse→fine; verifica a schermo (render salvati) a ogni step rischioso.
- Essendo **parametrico**, in re-iterazione spesso basta **modificare le quote** di
  sketch/feature esistenti invece di ricostruire da zero (riparti da `seed.f3d`).
- Salva snapshot `.f3d` a ogni step (storico) e, per le metriche, esporta STL e ri-verificalo.

## ✅ Validato su Fusion reale — run 2026-05-31 (porta-lattina / can caddy)
Prima run end-to-end confermata sul Mac (Fusion 2702.1.58, tool MCP reale
`mcp__fusion__fusion_mcp_execute`, NON `mcp__fusion__execute_fusion_code` come nel testo skill —
il render si fa con `app.activeViewport.saveAsImageFile`). 6 step, tutti i body creati, gate
overall=60 (reiterate). Gotcha **confermati e nuovi**:

- **⚠️ Il doc nuovo "Senza titolo" parte spesso in DIRECT MODELING mode** (timeline vuota,
  si comporta come "Part"/storia disattivata). Conseguenze viste in prima persona:
  - `root.occurrences.addNewComponent(...)` **NON supportato** → costruisci i body nel
    **root component** e **rinominali** (`body.name = 'Sleeve'`). Tutte le parti restano coassiali
    all'origine (intento relativo preservato lo stesso, niente matrici mondo).
  - `extrudeFeatures` con `CutFeatureOperation` **non trova il body target** in direct mode →
    workaround: estrudi come **`NewBodyFeatureOperation`** poi **`CombineFeatures` con
    `CutFeatureOperation`** (tool come strumento di taglio).
  - `CircularPatternFeature` **di feature** fallisce (`PATTERN_FEATURES_NO_PASTE_INT_EDGES`) →
    fai il **pattern di BODIES** (inputEntities = ObjectCollection di `bRepBodies`), asse =
    `root.zConstructionAxis`. Idem per i rod: pattern del body 'Rod' ×8 a 360°.
  - **FIX consigliato al preflight (§0):** su doc fresco imposta
    `des.designType = adsk.fusion.DesignTypes.ParametricDesignType` **prima** di modellare, così
    timeline/parametrico, `addNewComponent`, extrude-cut e pattern di feature funzionano in modo
    normale. (Da provare alla prossima run; in questa abbiamo lavorato in direct mode coi
    workaround sopra e ha retto.)
- **Sketch sul piano XZ: `sketchY = −worldZ`.** Per posizionare un profilo a quota world Z=12.3
  cm, il centro nello sketch va a `sketchY = −12.3`. Verifica sempre con la bbox del risultato.
- **Traforo (honeycomb) su parete cilindrica:** piano di costruzione **tangente** (offset del
  piano XZ a `y = raggio_esterno`), sketch dell'esagono lì, **extrude-cut a profondità FINITA**
  (~0.4–0.5 cm verso −Y) così tagli solo la parete vicina e **non** quella opposta; poi
  rectangular pattern verticale + **circular pattern di body** attorno a Z. Tieni i conteggi
  moderati (~13 colonne × 3 file) per il budget ~30s; converge meglio spezzando cut e pattern in
  chiamate separate con snapshot.
- **🛑 Honeycomb = UN SOLO set di esagoni in file a mattoncino, NON due set sovrapposti.** Per il
  nido d'ape l'offset tra file adiacenti e SOLO orizzontale (mezzo passo), sempre lo *stesso* set
  di celle. Errore visto (run 2026-05-31 iter3): aggiungere un **secondo set** di esagoni sfalsato
  di mezzo passo in z *e* in angolo → il secondo set cade esattamente sui setti del primo e li
  **mangia**, lasciando una parete svuotata con frammenti "a farfalla" disconnessi (non un
  traliccio). Dimensiona così che **passo > dimensione esagono** (setti residui ≥ ~0.3 cm) e
  verifica sul render che resti un **reticolo connesso** (non vuoto, non pieno). Se vedi frammenti
  volanti → esagoni troppo grandi/sovrapposti: riduci la cella o aumenta il passo e rifai dal
  solido. Per "traliccio" (celle grandi, parete per lo piu aperta) servono comunque setti integri.
- **Cut multi-target che lascia un tool fantasma.** Combine-Cut con `isKeepToolBodies=True` su piu
  body (es. 8 astine) lascia il **tool body** nel modello: non e cancellabile finche i combine lo
  referenziano (`deleteMe` ritorna True ma no-op; editare il combine da `3 : Didn't roll editing
  feature back`). Il modello qui si comporta **parametrico** (cancellare gli 8 combine ripristina
  le astine a z pieno). Soluzione pulita: ri-tagliare con il tool **consumato sull'ULTIMO** combine
  (`isKeepToolBodies=False` solo sull'8°) → nessun tool residuo. Importante per export: un tool
  fantasma finisce in `final.stl/step`.
- **Camera/render confermati:** la convenzione `VIEW_CAMERA` funziona; `cam.isFitView=True` +
  riassegnazione `vp.camera = cam` + `vp.refresh()` prima di `saveAsImageFile` dà render
  deterministici allineati ai `refs/`. (Tolto il dubbio "[da confermare]" sulla riassegnazione.)
- **Export confermati:** `createFusionArchiveExportOptions(path)` (.f3d),
  `createSTEPExportOptions(path, root)` (.step), `createSTLExportOptions(root, path)` con
  `meshRefinement=MeshRefinementMedium` (.stl). Tutti via `des.exportManager.execute(opts)`.
- **Watertight in assieme di body disgiunti:** il modello è un insieme di **solidi separati**
  (Base, Sleeve, 8×Rod, TopRing, Strap), ognuno `isSolid=True` ma **non fusi** tra loro. Per
  "watertight" ha senso il check per-body (B-rep solido), non un singolo STL unito; se serve un
  unico guscio stampabile, valuta un Combine-Join finale (non fatto qui).
- **Gap di fedeltà tipici visti dal gate** (utili come checklist di rifinitura): anello superiore
  a sezione troppo grossa (usa tubo Ø~3–4 mm o band rettangolare), sleeve troppo alta (i rod
  devono sporgere ~15–20% sopra), cinghietta da fare a **D piatta** (sweep di arco a sezione
  rettangolare) non o-ring tondo, fascia bassa solida troppo sottile, base poco rastremata.

## ⚠️ Dinamiche di re-iterazione e limiti del giudice (run 2026-05-31, iter2)
- **Il punteggio puo CALARE in re-iterazione** se si *overcorregge* un gap. Iter1: sleeve "troppo
  alta" → in iter2 abbassata a z7.8 → iter2: sleeve "troppo corta" (over-shoot opposto), overall
  60→54. Lezione: correggi i gap in modo **incrementale e misurato** (qui bastava ~−5 mm, non
  −12 mm); cambia **una variabile per volta** e riconfronta col ref PRIMA di spingere oltre.
- **Il giudice (Sonnet visivo) e rumoroso/inconsistente tra iterazioni** su quote relative
  (altezza sleeve, sporgenza rods): puo invertire la raccomandazione. Non trattare il suo numero
  come ground-truth fine; ancora le quote alle **proporzioni misurate sul ref**, non solo al
  commento del giudice.
- **Il render grigio flat-shaded nasconde le sezioni piatte:** una cinghietta resa come anello
  *piatto* (extrude 2 mm) viene letta come **o-ring tondo** dal giudice in grigio uniforme — gap
  "forma strap" segnalato aperto anche con geometria corretta. Mitigazione: render del gate in
  **shaded-with-edges** (spigoli visibili) cosi spessori e facce piatte si distinguono; valuta
  una posa 3/4 piu radente sulla feature in esame.
- **Allineamento rods↔ring:** se i rod arrivano a z_top e il ring e centrato piu in basso, i rod
  *superano* il ring (letto come errore di relazione `on_top_of`). Fai terminare i rod **alla
  quota del ring** (o alza il ring), non oltre.

## 📚 Pattern da tutorial — working-spring (2026-06-01, gate 77)
> Distillazione da `fusion-build-from-transcript` sul tutorial "Model a WORKING Spring in Fusion 360".
> I `candidate_lessons` del tutorial (split coil → revolute joint, components-before-joints,
> motion-link, joint-limit+rest, as-built joint, ground) **NON sono stati salvati**: tutti pattern
> di ASSEMBLY/MOTION, applicabili solo agli step 4–5 che sono **falliti** (filtro evidence-based:
> mai applicati con successo). Sono però emersi 3 **gotcha di backend confermati** in questo run:

- **Documento PART = un solo componente → niente joint/assiemi via MCP.** Il server apre un
  documento di tipo `DesignProductType` (Part), che può contenere **solo il rootComponent**:
  `occurrences.addNewComponent()` fallisce ("i documenti parte possono contenere un solo
  componente"), e `Joints.add()` dà "Provided input paths for joint are not valid" perché le due
  `JointGeometry` devono stare in **occorrenze diverse**. Quindi tutto lo strato assembly/motion
  (revolute/slider/rigid joint, motion-link, joint-limit, rest, ground) **non è realizzabile**:
  servirebbe un documento Assembly. `add_joint` granulare accetta solo `(component_one,
  component_two, joint_type)` — nessuna geometria/asse/flag/limite. _Validato_: step 4 (failed) —
  split riuscito (9 segmenti) ma joint impossibili; split annullato per preservare la geometria.
  Fonte: "you can't apply joints to bodies these all need to be components".
- **`extrude direction='negative'` esce SYMMETRIC (bug del tool granulare).** Produce
  un'estrusione a due lati (es. 2 mm invece di 1 mm a un solo lato). Per un solo lato usare
  `execute_code` con `setOneSideExtent(...)` + `ExtentDirections.NegativeExtentDirection` — oppure
  sketchare già sul piano alla quota giusta ed estrudere `positive`. _Validato_: step 2 (done,
  2 tentativi).
- **Nessun tool `coil`; `CoilFeatures.add` non disponibile → fallback sweep.** La collection
  `coilFeatures` espone solo `count/item/itemByName`. Fallback funzionante: **sweep** di un
  cerchio-profilo lungo una spline 3D elicoidale per punti (`r(t)=r0+z·tan(taper)`, `z` 0→h,
  `theta` 0→rev·2π) con piano profilo via `ConstructionPlaneInput.setByDistanceOnPath`. Export
  intero design: `exportManager.createSTEPExportOptions(path, root)` /
  `createSTLExportOptions(root, path)` (i metodi sono `*ExportOptions`, non `*ExportInput`).
  _Validato_: step 1 (done) — coil conica, bbox 12×12×12 mm a target.

## 📚 Pattern da tutorial — snap-fit-enclosure (2026-06-01, gate 82)
> Distillazione da `fusion-build-from-transcript` sul tutorial "easy snap fit enclosures" (case + top
> cover parametrici, 8/8 step done). Dei 9 candidate_lessons, scartati come **duplicati** già qui:
> mirror-feature, sketch fully-constrained, shell→pareti sottili; e `verify-clearance-with-section`
> (mai applicato nel build). I 5 pattern di **metodo** nuovi e validati:

- **Definisci gli user parameters all'inizio e guida le quote con espressioni** (width/length/height/
  shell/gap), non numeri sparsi. Ri-dimensioni l'intero modello cambiando un valore; le relazioni
  (es. altezza nub = `shell*2`, larghezza = `width/2`) restano coerenti. _Validato_: step 1 (done);
  fonte: "create some parameters to make it easier for us to adjust this design".
- **Per gli accoppiamenti con tolleranza usa un offset guidato da `shell+gap`.** Esprimere la
  clearance come somma parametrica (qui 1.5+0.2 = 1.7 mm) rende il fit di stampa esplicito e
  regolabile da un solo parametro. _Validato_: step 7 (done, lip offset interno); fonte: "driven with
  this shell and i want to add gap to that shell and that'll make it 1.7".
- **Usa un taper di -45° nelle estrusioni di feature sporgenti/incavi per renderle stampabili senza
  supporti.** Le FDM gestiscono bene gli overhang a 45°. Nota backend: il tool granulare `extrude`
  non ha taper → si fa via `execute_code` (`setOneSideExtent(..., taperAngle)`). _Validato_: step 4
  (nub) e step 8 (cutout), done; fonte: "negative 45 degree because 3d printers can print 45 degree
  features".
- **Guida la geometria di una parte da quella di un'altra: riusa facce, profili e sketch tra le
  parti invece di ridisegnarli.** Cover, lip e cutout derivano dai profili/facce del case e dallo
  sketch del nub → restano coerenti e si aggiornano da soli, con meno sketch. _Validato_: step 7
  (lip dal perimetro del cover), done; fonte: "leverage all the kind of sketches ... from other
  components ... you don't need to make a whole bunch of sketches".
- **Ancora l'inizio di un'estrusione a geometria reale con lo start 'from object'** (da una faccia
  esistente) invece che al piano dello sketch, quando l'inizio deve seguire una superficie. _Validato_:
  step 8 (cutout dal piano del lip), done; fonte: "change the start ... to from object ... start from
  this surface".

## 🔎 Gate: fit-check di sezione + compenetrazione (regola, 2026-06-01)
> Indicazione utente, da applicare SEMPRE negli assiemi multi-parte. Un render esterno può
> sembrare perfetto mentre internamente le parti interferiscono.

- **Caso che l'ha motivata:** enclosure snap-fit — gate esterno 82/100 ma coperchio e case erano in
  **compenetrazione solida** (nessuna luce), quindi il lock non poteva funzionare. Visibile solo con
  sezione + check di interferenza.
- **Aggiungi al gate (§6), oltre a bbox/simmetria/watertight:**
  - **Interferenza per coppia di corpi**: `tbm=TemporaryBRepManager.get(); c=tbm.copy(a); tbm.booleanOperation(c, tbm.copy(b), IntersectionBooleanType)` → `c.volume` deve essere ~0. In alternativa, **mappa di occupazione** per punti sul piano di accoppiamento (`body.pointContainment` su una griglia (x,z) a y=0): 0 celle in comune. Qualsiasi overlap = **difetto HARD**, a prescindere dal punteggio visivo.
  - **Render di sezione** attraverso le feature di mating: taglio temporaneo della metà anteriore (box + `combineFeatures` CutFeatureOperation su entrambi i corpi), camera front ortho, visual style **shaded-with-edges** per leggere i canali di luce; poi **ripristina** cancellando le feature temporanee.
- **Gotcha:** il tool `mcp__fusion__create_section_analysis` è bacato (`root.analyses` inesistente):
  fai la sezione via `execute_code`. E **cancella la section analysis residua** da `design.analyses`
  a fine controllo, altrimenti taglia (hatch) tutti i render successivi del viewport.
- **API utile:** `offsetFacesFeatures.createInput(<lista di BRepFace>, ValueInput.createByReal(d))`
  (lista Python di facce, NON ObjectCollection; niente arg isChain) per ingrandire un corpo di una
  luce uniforme → utile a ricavare un accoppiamento con clearance come complemento booleano.
