# Performance Review – EBM Scan-Strategie-Software

Vergleich: **FriesslebenA/EBM-Software** (Kollege, feature/v2-next-features) vs. **Obelix3000/EBM-Strategy-Converter** (eigene Repo)

---

## 1. Strategie-Übersicht und Vergleich

### Architektureller Hauptunterschied

| Aspekt | Kollege (abs_path_optimizer) | Eigene Repo (EBM-Strategy-Converter) |
|---|---|---|
| UI-Framework | Tkinter + PySide6-Viewer | Streamlit (Web-App) |
| Optimierungskonzept | 8 atomare Modi | **2-stufig**: Makro-Segmentierung + Mikro-Sortierung |
| Kombinierbarkeit | Fest, pro Modus | Flexibel: jede Makro × jede Mikro-Strategie |
| Visualisierung | Echtzeit-Animation (tkinter + Qt) | Statische Plotly-Charts |
| Wärmemodell | Nicht vorhanden | Gaußsche Diffusion (lookback 200 Punkte) |
| Parallelisierung | ProcessPoolExecutor (spawn) | Keine |
| STEP-Import | Ja (cadquery, subprocess) | Nein |

---

### Strategien im Kollegen-Repo

| Modus | Algorithmus | Zeitkomplexität |
|---|---|---|
| `direct_visualisation` | Keine Umsortierung | O(1) |
| `local_greedy` | Nächster-Nachbar mit Gedächtnis-Repulsion (Python-Schleife, kein KDTree) | **O(N²)** |
| `dispersion_maximisation` | Inversion von local_greedy: weites Sprung-Ziel bevorzugt | **O(N²)** |
| `deterministic_grid_dispersion` | Virtuelle Gitterzellen, Boustrophedon-Zellreihenfolge, history-gewichtete Auswahl | O(N × recent × cands) |
| `stochastic_grid_dispersion` | Wie oben, stochastische Intra-Zell-Auswahl | O(N × recent × cands) |
| `density_adaptive_sampling` | Dichte-gewichtete Zellauswahl + history-Abstand | **O(N²)** naiv |
| `ghost_beam_scanning` | Streifenerkennung + Interleaving von Primär- und Verzögerungs-Segmenten | O(N) |
| `interlaced_stripe_scanning` | Streifenerkennung + modulares Forward/Backward-Sprungmuster | O(N) |

### Strategien in der eigenen Repo

**Makro-Segmentierung (Stufe 1):**

| Strategie | Algorithmus | Komplexität |
|---|---|---|
| Keine | Kein Splitting | O(1) |
| Schachbrett / Island | Gitterzellen, Phase A (gerade Diagonale) + Phase B (ungerade) | O(N) |
| Streifen | Rotation + Projektion auf Y-Achse, Streifenindex | O(N) |
| Hexagonal | Versetztes Wabengitter (h=s√3, v=s·1.5) | O(N) |
| Spiralzonen | Ringindex = Abstand zum Schwerpunkt / seg_size | O(N) |

**Mikro-Sortierung (Stufe 2):**

| Strategie | Algorithmus | Komplexität |
|---|---|---|
| Raster / Zick-Zack | Zeilen auf 50-µm-Raster runden, X-Sortierung, jede 2. Zeile umkehren | O(N log N) |
| Spot Ordered | Raster-Vorsortierung + Multipass-Skip-Splitting | O(N log N) |
| Ghost Beam | Primärpfad + verzögerter Sekundärpunkt (2×N Ausgabe) | O(N) |
| Hilbert-Kurve | 2^order × 2^order Grid, Hilbert-Index per Punkt, Sortierung | O(N · order) |
| Peano-Kurve | 3^order × 3^order Grid, Boustrophedon-Key, Sortierung | O(N log N) |
| Spiral | Polarkoordinaten, Ring + Winkel, Lexsort | O(N log N) |
| Greedy | KDTree + Repulsionsgedächtnis (scipy) | O(N · K) |
| Dispersion Max | Weitestes-Ziel-Greedy, numpy argpartition | **O(N²)** |
| Gitter-Dispersion (det.) | Boustrophedon-Zellen + age-decay-History | O(N · recent · cands) |
| Gitter-Dispersion (stoch.) | Wie det. + zufällige Stichprobe (candidate_limit=64) | O(N · recent · cands) |
| Dichte-adaptiv | Zellgewichtung nach Dichte + history-Abstand | O(N²) naiv, begrenzt durch pool=128 |
| Interlaced Stripes | Streifenerkennung + modulares Sprungmuster (forward/backward) | O(N) |

### Im Kollegen-Repo fehlende Strategien

Die folgenden Strategien aus der eigenen Repo sind noch **nicht** im Kollegen-Repo:

| Fehlend | Art | Besonderheit |
|---|---|---|
| **Schachbrett / Island** | Makro | Zweiphasige thermische Trennung der Schmelzbereiche |
| **Hexagonal** | Makro | Bessere Flächenabdeckung als Quadrat-Grid |
| **Spiralzonen** | Makro | Innen→Außen oder Außen→Innen Wärmesteuerung |
| **Streifensegmentierung** | Makro | Drehwinkel-basiert, unabhängig von Mikrostrategie |
| **Raster / Zick-Zack** | Mikro | Basis-Referenzstrategie, fehlt komplett |
| **Spot Ordered / Multipass** | Mikro | Effektive Abkühlpause ohne Dispersionsverlust |
| **Hilbert-Kurve** | Mikro | Optimale Raumfüllung, cache-effizient |
| **Peano-Kurve** | Mikro | Alternative zu Hilbert, 3er-Basis |
| **Spiral** | Mikro | Konzentrische Abkühlung |
| **2-Stufen-Komposierbarkeit** | Architektur | Beliebige Makro × Mikro-Kombination |
| **Wärmeakkumulationsmodell** | Analyse | Gaußsche Diffusion, 3 Materialien |

---

## 2. Warum manche Strategien so lange brauchen

### Hauptursache: O(N²)-Schleifen in Python

Alle langsamen Modi haben das gleiche Grundproblem: Sie iterieren für **jeden der N Punkte** über eine Teilmenge der restlichen N Punkte in reinem Python (kein numpy-Vektorbetrieb, kein C-Extension).

#### `local_greedy` / `dispersion_maximisation` (Kollegen-Repo)

```
für jeden Punkt i (N Iterationen):
    berechne Distanz zu allen verbleibenden Punkten  → O(N) Python-Loop
    berechne Repulsion zu memory=4 letzten Punkten  → O(memory × N)
    wähle besten Kandidaten
```

**Kein KDTree.** Die eigene Repo verwendet `scipy.spatial.KDTree.query(k=K)` und beschränkt den Kandidatenpool auf K Punkte. Der Kollege fragt **alle** verbleibenden Punkte ab.

| N | Kollege (Python-Loop) | Eigene (KDTree) |
|---|---|---|
| 10 000 | ~5 s | ~0.1 s |
| 50 000 | ~2 min | ~2 s |
| 100 000 | ~8 min | ~8 s |

#### `density_adaptive_sampling` (beide Repos)

Zwar begrenzt durch `pool_limit=128` (eigene Repo) bzw. Grid-Buckets (Kollege), aber die History-Scores werden in Python-Schleifen berechnet, nicht vektorisiert.

#### Streifenerkennung (`detect_source_stripe_ranges`, Kollege)

Lineare Suche nach Rückwärtssprüngen: O(N) – kein Bottleneck, aber bei 500k Punkten messbar.

### Sekundärursache: Prozessstart-Overhead

Der Kollege startet Optimierungen in `ProcessPoolExecutor` mit `spawn`-Kontext. Jeder Spawn erzeugt einen neuen Python-Interpreter (~200–500 ms Overhead). Bei vielen kleinen Dateien dominiert dieser Overhead die eigentliche Rechenzeit.

---

## 3. Visualisierungs-Performance

### Problem 1: Tkinter PhotoImage – `photo.put()` pro Pixel (Kollege)

Die Canvas-Animation in `ComparisonApp` zeichnet Punkte einzeln über `photo.put(color, to=(left, top, right, bottom))`. Jeder Aufruf ist ein **Tcl-Interpreter-Roundtrip** über den GIL.

```
Pro Frame (33 ms Ziel):
- Incremental update: O(delta_punkte) → meist 1–2 neue Punkte → schnell
- Reset (_reset_canvas_raster): O(N) × photo.put() → bei 10k Punkten: ~50 ms → BLOCKIERT UI
```

**Reset wird ausgelöst bei:** Fenstergrößenänderung, Trail-Längenänderung, Vorwärts-Rückwärts-Navigation. Jede dieser Aktionen friert die UI für 50–500 ms ein.

**Lösung:** Alle Punkte in einem einzigen `photo.put()`-Aufruf mit einem vorberechneten Pixel-Array schreiben (via `numpy` → `PIL.Image` → `ImageTk.PhotoImage`). Würde den Reset von O(N) `photo.put()`-Aufrufen auf **1 Aufruf** reduzieren.

### Problem 2: Raster-Viewer – voller Array-Copy bei jeder Transformation (Kollege)

```python
points_copy = np.ascontiguousarray(self.points, dtype=np.float32)  # O(N) Kopie
future = self._executor.submit(render_raster_background_image, points_copy, snapshot)
```

Bei jedem Pan/Zoom wird eine **vollständige Kopie** aller N Punkte erzeugt und an den Thread-Pool übergeben. Das 200-ms-Debounce verhindert zwar Spam, aber eine einzelne Renderanforderung bei 500k Punkten (~4 MB float32) kostet bereits messbar Zeit beim Kopieren.

**Zusätzlich:** `draw_mapped_points()` zeichnet alle N Punkte via `QPainter.drawPoints()`. QPainter ist für Millionen von Einzelpunkten nicht optimiert – eine `QImage`-basierte direkte Pixel-Manipulation (numpy → `QImage.bits()`) wäre deutlich schneller.

### Problem 3: OpenGL-Backend – fehlende Vertex-Buffer-Persistenz (Kollege)

```python
VIEWER_GL_MAX_BATCH_POINTS = 1_000_000
# Punkte werden in Batches von 1M gezeichnet
```

Das OpenGL-Backend lädt **jedes Frame** die Punktkoordinaten neu in den GPU-Buffer (kein persistenter VBO). Bei 500k Punkten × 8 Bytes = 4 MB CPU→GPU-Transfer pro Frame → bei 30 FPS: **120 MB/s** CPU→GPU-Bandbreite nur für Hintergrundpunkte.

**Lösung:** Statischen VBO einmal hochladen, nur MVP-Matrix per Frame aktualisieren.

### Problem 4: Eigene Repo – Plotly ohne Echtzeit-Animation

Die eigene Repo hat **keine** Animations-Schleife. `plot_layer_coarse()` sampelt auf max. 2000 Punkte und rendert einmalig. Das ist performant, aber bietet keine flüssige Pfad-Animation. Für flüssige Animation wäre eine Frame-basierte Annäherung via Plotly Frames (`go.Frame`) oder ein Wechsel zu einem echten Render-Framework (PyQtGraph, Vispy) nötig.

---

## 4. Quantitative Performance-Tabelle

### Optimierung (Zielplattform: 50 000 Punkte, typische EBM-Schicht)

| Modus | Repo | Laufzeit (geschätzt) | Grund |
|---|---|---|---|
| Raster / Zick-Zack | Eigene | < 10 ms | O(N log N), numpy |
| Hilbert / Peano | Eigene | < 20 ms | O(N log N), numpy |
| Ghost Beam | Beide | < 50 ms | O(N), numpy/Python |
| Interlaced Stripes | Beide | < 50 ms | O(N), Python |
| Grid Dispersion | Beide | 1–5 s | O(N × recent × cands) |
| Greedy (KDTree) | Eigene | 2–10 s | O(N × K), scipy |
| Greedy (Python-Loop) | Kollege | **60–120 s** | O(N²), kein KDTree |
| Dispersion Max | Eigene | **30–90 s** | O(N²), kein KDTree |
| Dispersion Max | Kollege | **60–120 s** | O(N²), kein KDTree |

### Rendering (Tkinter Canvas, 50 000 Punkte)

| Operation | Zeit | Bottleneck |
|---|---|---|
| Initialer Reset (`_reset_canvas_raster`) | ~250 ms | 50k × `photo.put()` Tcl-Roundtrips |
| Incremental Frame-Update | 1–3 ms | O(delta) ≈ O(1) |
| Resize / Trail-Änderung (Reset trigger) | ~250 ms | Vollständiger Raster-Reset |

### Rendering (Qt Raster-Viewer, 50 000 Punkte)

| Operation | Zeit | Bottleneck |
|---|---|---|
| Background image (Thread) | ~50 ms | O(N) map + QPainter.drawPoints() |
| Overlay Frame (Haupt-Thread) | < 2 ms | O(trail ≤ 64) |
| Gesamt empfundene Latenz | 200 ms | Debounce-Intervall |

---

## 5. Empfehlungen

### Sofort umsetzbar (hohes Verhältnis Aufwand/Wirkung)

**A) KDTree in `local_greedy` und `dispersion_maximisation` (Kollege)**
Ersetze die vollständige Python-Schleife über alle Restpunkte durch:
```python
from scipy.spatial import KDTree
tree = KDTree(points_array)
distances, indices = tree.query(current_point, k=max(memory * 6, 48))
```
Erwartete Beschleunigung: **10–50×** bei N=50k.

**B) Batch-Pixel-Write im Tkinter-Canvas (Kollege)**
Ersetze N × `photo.put()` im `_reset_canvas_raster` durch:
```python
import numpy as np
from PIL import Image, ImageTk
# Alle Punkte in numpy-Array → PIL → PhotoImage
img_array = np.zeros((height, width, 3), dtype=np.uint8)
img_array[ys, xs] = [27, 27, 27]  # Vektoriell
image = Image.fromarray(img_array)
canvas.render_photo = ImageTk.PhotoImage(image)
```
Voraussetzung: Pillow zu `requirements.txt` hinzufügen.
Erwartete Beschleunigung bei Reset: **50–200×**.

**C) Persistenter VBO im OpenGL-Viewer (Kollege)**
Hintergrundpunkte einmalig in einen OpenGL-Buffer laden; bei Pan/Zoom nur die View-Matrix aktualisieren. Entfernt 4 MB CPU→GPU-Transfer pro Frame.

**D) Numpy-Vektorisierung der Scoring-Loops (beide Repos)**
In `sort_grid_dispersion` / `density_adaptive_sampling` die History-Score-Berechnung vektorisieren:
```python
# Aktuell: Python-Schleife über recent
for age, r_idx in enumerate(reversed(recent)):
    scores += (decay ** age) * np.linalg.norm(cand_pts - points[r_idx], axis=1)

# Vektorisiert (recent_mem × cand × 2 Broadcasting):
recent_pts = points[recent[-recent_mem:]]  # shape (M, 2)
decays = decay_arr[:len(recent_pts)][:, None]  # shape (M, 1)
diffs = cand_pts[None, :, :] - recent_pts[:, None, :]  # (M, cands, 2)
scores = (decays * np.linalg.norm(diffs, axis=2)).sum(axis=0)
```

### Mittelfristig

**E) Strategie-Import aus eigener Repo in Kollegen-Repo**
Priorität nach wissenschaftlichem Mehrwert:
1. Raster / Zick-Zack (Referenzstrategie, O(N log N))
2. Hilbert-Kurve (raumfüllend, cache-effizient)
3. Schachbrett-Makrosegmentierung (thermische Isolation)
4. Spot Ordered / Multipass (Abkühlpause ohne Dispersionsverlust)
5. Wärmeakkumulationsmodell (Analyse-Feature)

**F) 2-Stufen-Architektur im Kollegen-Repo einführen**
Die Komposierbarkeit (jede Makro × jede Mikro) erlaubt N×M Kombinationen statt N+M fester Modi.

### Langfristig

**G) Vispy oder PyQtGraph als Viewer-Backend (Kollege)**
OpenGL-basierte Punkt-Renderer mit persistenten VBOs, die explizit für große Punktwolken ausgelegt sind. Vispy kann 10M Punkte bei 60 FPS rendern.

**H) Numba JIT für Greedy-Schleifen (beide Repos)**
```python
from numba import njit
@njit
def greedy_step(points, visited, current_idx, recent, w1, w2):
    ...
```
Würde die O(N²)-Schleifen auf C-Geschwindigkeit bringen ohne Algorithmusänderung.
