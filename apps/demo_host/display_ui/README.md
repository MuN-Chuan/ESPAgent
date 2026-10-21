# ESPAgent 240×320 Web Display UI

Detailed project documentation:

- `docs/display-ui-design-experience.md`: design constraints, experiments, failure modes and acceptance checklist.
- `docs/display-ui-features-and-operations.md`: features, controls, API, extension workflow and troubleshooting.

The ESP32 product scenes use a theme registry so complete UI systems can be switched without changing the transport layer:

- `index.html`: shared 240×320 shell and theme resource loading.
- `screen.css`: shared reset, palette and global motion override.
- `screen.js`: theme selection, state polling and common rendering utilities.
- `themes/<id>/theme.js`: theme markup, state mapping and state-change effects.
- `themes/<id>/theme.css`: isolated layout, typography and motion design.
- `themes/<id>/assets/`: local SVG, bitmap and font resources owned by that theme.

The registered themes are `first` (Paper, the default) and `second` (Editorial). Switch at runtime with:

```http
POST /api/display/ui
Content-Type: application/json

{"theme":"second","font_chinese":"editorial","font_latin":"industrial","font_numeric":"mono","scale_percent":105,"font_hinting":"smooth","grayscale_antialiasing":true,"motion_enabled":true}
```

## Font architecture

Fonts use a hybrid model: resources and font roles are registered globally in `fonts/fonts.css`, while every theme owns a per-role allowlist. Mixed strings are segmented into Han, Latin, and numeric runs so each script receives its selected typeface instead of being captured by the first font in a fallback stack.

| Role | Font ids |
| --- | --- |
| Chinese | `auto`, `modern`, `ui`, `editorial`, `humanist`, `pixel` |
| English | `auto`, `modern`, `ui`, `editorial`, `industrial`, `pixel` |
| Numeric | `auto`, `modern`, `editorial`, `industrial`, `mono`, `pixel` |

The first theme accepts every registered font in every role. The second theme excludes only `humanist` Chinese because its metrics do not fit the Editorial layout. The legacy `font` request field remains available as a shortcut preset; role-specific fields take precedence.

Ark Pixel Font `2026.07.01` is distributed under SIL Open Font License 1.1. The bundled Simplified Chinese proportional WOFF2 and its `OFL.txt` are stored in `fonts/ark-pixel/`. Browser comparison supports `/screen/?theme=first&font_zh=pixel&font_en=industrial&font_num=mono`.

Font hinting supports `auto`, `crisp`, and `smooth`. Optional grayscale antialiasing converts browser coverage to the neutral Paper-to-Ink ramp after removing RGB subpixel fringes; it is off by default so the existing exact two-color output is preserved.

## Global scale

`scale_percent` supports `90`, `95`, `100`, `105`, and `110`. Scaling uses an adaptive logical canvas: the physical output remains 240×320, while the theme stage receives the inverse logical width and height before being scaled to the screen. Theme content widths, scene heights, text wrapping and link metrics therefore reflow instead of being cropped.

The browser preview accepts a non-persistent override such as `/screen/?theme=first&font_zh=pixel&font_en=industrial&font_num=mono&scale=110`.

Open `http://127.0.0.1:8787/screen/` while the Demo Host is running to inspect the UI directly. The page polls the existing state API in browser-preview mode. At runtime, `WebDisplayRenderer` opens the same file in a persistent headless Chrome process, injects `DemoState`, captures a 240×320 screenshot, reduces it to either the exact Paper/Ink palette or the optional neutral antialiasing ramp, and passes the image into the existing Tone and ESP32 transport pipeline.

For design-only comparison, query parameters override runtime selection without changing device state:

- `/screen/?theme=first`
- `/screen/?theme=second`
- `/screen/?theme=second&motion=0`

## Display constraints

- Fixed viewport: `240 × 320` CSS pixels at device scale 1.
- Product palette: Paper `#F8FAFC` and Ink `#111827` only.
- Use font size, weight, spacing and one-pixel rules for hierarchy.
- Avoid gradients, shadows, opacity and thin colored text; these increase MJPEG artifacts.
- Keep all assets local. Runtime rendering must not require internet access.
- Animate only scene entry or discrete state changes. Never run decorative infinite animations because every frame increases MJPEG traffic.
- The first theme uses interruptible Web Animations for directional page reveals and staggered content motion. Theme `unmount()` must cancel any active animations before its DOM is removed.
- `hermes`, `lights` and `link` are Web scenes. Pixel test patterns and video intentionally remain on the Pillow path.

After editing, refresh `/screen/` for browser inspection and restart the Demo Host so its headless renderer reloads the files.
