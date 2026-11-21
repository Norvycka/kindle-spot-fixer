#!/usr/bin/env python3
"""
Kindle Spot Fixer - lay out ebooks so the text flows AROUND a damaged
part of your Kindle screen.

Start the app (opens in your web browser, works offline):
    python kindle_spot.py

  1. pick your Kindle model
  2. drag a box over the damaged area
  3. choose books and convert - optionally copied straight onto the Kindle

Quick convert using your last saved settings:
    python kindle_spot.py book1.epub book2.epub

Install once:  pip install reportlab beautifulsoup4 lxml
MOBI/AZW3 input also needs Calibre installed. DRM-free books only.
"""
import argparse, os, re, shutil, subprocess, sys, tempfile, zipfile
import posixpath, warnings
from bs4 import XMLParsedAsHTMLWarning
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, NavigableString, Tag
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate,
                                Paragraph, Spacer, PageBreak)
from reportlab.pdfgen import canvas as rl_canvas

# Physical screen sizes (inches). PDF page = real screen size, so 11pt text
# on the PDF really looks like 11pt on the Kindle.
# name: (width px, height px, ppi). PDF page = real physical screen size,
# so 11pt text in the PDF really looks like 11pt on the Kindle.
MODEL_SPECS = {
    "Kindle 7th gen (2014)":            (600, 800, 167),
    "Kindle 8th gen (2016)":            (600, 800, 167),
    "Kindle 10th gen (2019)":           (600, 800, 167),
    "Kindle 11th gen (2022)":           (1072, 1448, 300),
    "Kindle 4 / 5 / Touch":             (600, 800, 167),
    "Paperwhite 1 / 2 (2012-2013)":     (758, 1024, 212),
    "Paperwhite 3 / 4 (2015-2018)":     (1072, 1448, 300),
    "Paperwhite 5 (2021)":              (1236, 1648, 300),
    "Paperwhite 12th gen (2024)":       (1264, 1680, 300),
    "Kindle Voyage":                    (1072, 1448, 300),
    "Kindle Oasis 2 / 3":               (1264, 1680, 300),
}
DEFAULT_MODEL = "Kindle 7th gen (2014)"


def page_size(model):
    w, h, ppi = MODEL_SPECS[model]
    return w / ppi * 72, h / ppi * 72


DEFAULT_SPOT = (0.70, 0.74, 0.92, 0.87)   # estimated from your photo, padded

FONT_CANDIDATES = [
    # (regular, bold, italic, bold-italic)
    ("C:/Windows/Fonts/georgia.ttf", "C:/Windows/Fonts/georgiab.ttf",
     "C:/Windows/Fonts/georgiai.ttf", "C:/Windows/Fonts/georgiaz.ttf"),
    ("C:/Windows/Fonts/times.ttf", "C:/Windows/Fonts/timesbd.ttf",
     "C:/Windows/Fonts/timesi.ttf", "C:/Windows/Fonts/timesbi.ttf"),
    ("/System/Library/Fonts/Supplemental/Georgia.ttf",
     "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
     "/System/Library/Fonts/Supplemental/Georgia Italic.ttf",
     "/System/Library/Fonts/Supplemental/Georgia Bold Italic.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf"),
]


def register_font(custom=None):
    """Register a TTF family that covers Lithuanian (ą č ę ė į š ų ū ž)."""
    sets = [custom] if custom else FONT_CANDIDATES
    for s in sets:
        if s and all(os.path.exists(p) for p in s):
            names = ["Body", "Body-Bold", "Body-Italic", "Body-BoldItalic"]
            for n, p in zip(names, s):
                pdfmetrics.registerFont(TTFont(n, p))
            pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold",
                                          italic="Body-Italic",
                                          boldItalic="Body-BoldItalic")
            return "Body"
    sys.exit("No suitable font found. Pass one with --font regular.ttf "
             "(bold/italic optional).")


# ---------------------------------------------------------------- EPUB reading
BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li",
              "blockquote", "pre", "section", "article", "body", "ul", "ol",
              "table", "tr", "td", "figure", "aside", "header", "footer"}
INLINE_MAP = {"i": "i", "em": "i", "cite": "i", "b": "b", "strong": "b"}


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def inline_markup(node):
    """Return reportlab-markup text for an element's inline content."""
    out = []
    for c in node.children:
        if isinstance(c, NavigableString):
            if c.__class__.__name__ in ("Comment", "ProcessingInstruction",
                                        "Doctype", "CData"):
                continue
            out.append(esc(str(c)))
        elif isinstance(c, Tag):
            name = c.name.lower()
            if name == "br":
                out.append("<br/>")
            elif name in ("script", "style", "img", "svg"):
                continue
            elif name in INLINE_MAP:
                inner = inline_markup(c)
                if inner.strip():
                    t = INLINE_MAP[name]
                    out.append(f"<{t}>{inner}</{t}>")
            elif name == "sup":
                out.append(f"<super>{inline_markup(c)}</super>")
            else:
                out.append(inline_markup(c))
    return "".join(out)


def walk_blocks(node, blocks):
    """Collect (kind, markup) blocks in document order."""
    has_block_child = any(isinstance(c, Tag) and c.name.lower() in BLOCK_TAGS
                          for c in node.children)
    name = node.name.lower() if isinstance(node, Tag) else ""
    if not has_block_child:
        text = re.sub(r"\s+", " ", inline_markup(node)).strip()
        if text:
            kind = "h" if re.fullmatch(r"h[1-6]", name) else (
                   "quote" if name == "blockquote" else "p")
            blocks.append((kind, text))
        return
    # mixed content: flush loose inline text as its own paragraphs
    buf = []
    for c in node.children:
        if isinstance(c, Tag) and c.name.lower() in BLOCK_TAGS:
            t = re.sub(r"\s+", " ", "".join(buf)).strip()
            if t:
                blocks.append(("p", t))
            buf = []
            walk_blocks(c, blocks)
        elif isinstance(c, Tag):
            if c.name.lower() not in ("script", "style"):
                wrapper = BeautifulSoup("<x></x>", "lxml").x
                wrapper.append(c.__copy__())
                buf.append(inline_markup(wrapper))
        elif isinstance(c, NavigableString):
            if c.__class__.__name__ == "NavigableString":
                buf.append(esc(str(c)))
    t = re.sub(r"\s+", " ", "".join(buf)).strip()
    if t:
        blocks.append(("p", t))


def read_epub(path):
    z = zipfile.ZipFile(path)
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container",
          "o": "http://www.idpf.org/2007/opf",
          "dc": "http://purl.org/dc/elements/1.1/"}
    container = ET.fromstring(z.read("META-INF/container.xml"))
    opf_path = container.find(".//c:rootfile", ns).get("full-path")
    opf = ET.fromstring(z.read(opf_path))
    base = posixpath.dirname(opf_path)
    title_el = opf.find(".//dc:title", ns)
    title = title_el.text.strip() if title_el is not None and title_el.text else None
    manifest = {i.get("id"): i.get("href")
                for i in opf.find("o:manifest", ns)}
    chapters = []
    for ref in opf.find("o:spine", ns):
        href = manifest.get(ref.get("idref"))
        if not href:
            continue
        full = posixpath.normpath(posixpath.join(base, href.split("#")[0]))
        try:
            html = z.read(full)
        except KeyError:
            continue
        soup = BeautifulSoup(html, "lxml")
        body = soup.body or soup
        blocks = []
        walk_blocks(body, blocks)
        if blocks:
            chapters.append(blocks)
    return title, chapters


def to_epub(path, tmpdir):
    """Convert MOBI/AZW3/etc. to EPUB with Calibre's ebook-convert."""
    exe = shutil.which("ebook-convert")
    for guess in (r"C:\Program Files\Calibre2\ebook-convert.exe",
                  "/Applications/calibre.app/Contents/MacOS/ebook-convert"):
        if not exe and os.path.exists(guess):
            exe = guess
    if not exe:
        sys.exit("To read MOBI/AZW3, install Calibre (https://calibre-ebook.com) "
                 "or convert the book to EPUB in Calibre first.")
    out = os.path.join(tmpdir, "book.epub")
    subprocess.run([exe, path, out], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out


# ---------------------------------------------------------------- layout
def build_frames(W, H, spot, margin, footer, min_h=30):
    """Frames that tile the page while skipping the spot rectangle."""
    l, t, r, b = spot
    L, R = margin, W - margin
    T, B = H - margin, margin + footer          # reportlab: y goes up
    sy_top = H * (1 - t)                         # spot top edge (y coord)
    sy_bot = H * (1 - b)                         # spot bottom edge
    sx_l, sx_r = W * l, W * r
    frames = []

    def add(x0, y0, x1, y1, fid):
        if x1 - x0 > 40 and y1 - y0 > 14:
            frames.append(Frame(x0, y0, x1 - x0, y1 - y0, id=fid,
                                leftPadding=0, rightPadding=0,
                                topPadding=0, bottomPadding=0))

    if T - sy_top >= min_h:
        add(L, max(sy_top, B), R, T, "above")
    # tiny strips that can't hold two lines get merged into the side frames
    if sy_bot - B < min_h:
        sy_bot = B
    if T - sy_top < min_h:
        sy_top = T
    mid_top, mid_bot = min(sy_top, T), max(sy_bot, B)
    if mid_top > mid_bot:
        add(L, mid_bot, min(sx_l, R), mid_top, "left")
        # a strip right of the spot is only used if it's wide enough
        add(max(sx_r, L), mid_bot, R, mid_top, "right")
    if sy_bot > B:
        add(L, B, R, min(sy_bot, T), "below")
    return frames


def make_pdf(title, chapters, out, W, H, spot, size, font):
    margin = 0.18 * inch
    footer = size * 1.1
    base = ParagraphStyle("body", fontName=font, fontSize=size,
                          leading=size * 1.35, alignment=TA_JUSTIFY,
                          firstLineIndent=size * 1.2, spaceAfter=size * 0.15,
                          allowWidows=1, allowOrphans=1)
    styles = {
        "p": base,
        "quote": ParagraphStyle("q", parent=base, leftIndent=size,
                                rightIndent=size, firstLineIndent=0),
        "h": ParagraphStyle("h", parent=base, fontName=font + "-Bold",
                            fontSize=size * 1.3, leading=size * 1.6,
                            alignment=TA_CENTER, firstLineIndent=0,
                            spaceBefore=size * 0.8, spaceAfter=size * 0.8),
    }

    def footer_fn(c, doc):
        c.saveState()
        c.setFont(font, size * 0.7)
        c.drawCentredString(W / 2, margin * 0.6, str(doc.page))
        c.restoreState()

    doc = BaseDocTemplate(out, pagesize=(W, H), title=title or "Book",
                          leftMargin=0, rightMargin=0, topMargin=0,
                          bottomMargin=0)
    doc.addPageTemplates([PageTemplate("p", frames=build_frames(
        W, H, spot, margin, footer, min_h=size * 1.35 * 2.2), onPage=footer_fn)])
    story = []
    if title:
        story += [Spacer(1, H * 0.25), Paragraph(esc(title), styles["h"]),
                  PageBreak()]
    for i, ch in enumerate(chapters):
        if i:
            story.append(PageBreak())
        for kind, text in ch:
            try:
                story.append(Paragraph(text, styles[kind]))
            except Exception:   # malformed markup: fall back to plain text
                plain = re.sub(r"<[^>]+>", "", text)
                story.append(Paragraph(plain, styles[kind]))
    doc.build(story)


def make_calibration(out, W, H, spot, font):
    c = rl_canvas.Canvas(out, pagesize=(W, H))
    n = 20
    for i in range(n + 1):
        x, y = W * i / n, H * i / n
        heavy = i % 5 == 0
        c.setLineWidth(0.8 if heavy else 0.2)
        c.line(x, 0, x, H)
        c.line(0, y, W, y)
    c.setFont(font, 5)
    for i in range(0, n, 1):
        for j in range(0, n, 1):
            # label = column letter + row number, from the top-left
            lab = f"{chr(65 + i)}{j + 1}"
            c.drawCentredString(W * (i + 0.5) / n, H * (1 - (j + 0.62) / n), lab)
    l, t, r, b = spot
    c.setLineWidth(1.5)
    c.setDash(3, 2)
    c.rect(W * l, H * (1 - b), W * (r - l), H * (b - t))
    c.showPage()
    c.save()


# ---------------------------------------------------------------- Kindle drive
def find_kindle():
    """Return the Kindle's 'documents' folder if a Kindle is plugged in."""
    import glob, string
    cands = []
    if os.name == "nt":
        cands = [f"{d}:\\" for d in string.ascii_uppercase[3:]]
    else:
        cands = (glob.glob("/Volumes/Kindle*") + glob.glob("/media/*/Kindle*")
                 + glob.glob("/run/media/*/Kindle*") + glob.glob("/media/Kindle*"))
    for root in cands:
        try:
            docs = os.path.join(root, "documents")
            if os.path.isdir(docs) and os.path.isdir(os.path.join(root, "system")):
                return docs
        except OSError:
            continue
    return None


# ---------------------------------------------------------------- settings
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".kindle_spot.json")
OUT_DIR = os.path.join(os.path.expanduser("~"), "Kindle spot books")


def load_settings():
    import json
    d = {"model": DEFAULT_MODEL, "spot": list(DEFAULT_SPOT), "size": 10.5}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            d.update(json.load(f))
    except (OSError, ValueError):
        pass
    if d["model"] not in MODEL_SPECS:
        d["model"] = DEFAULT_MODEL
    return d


def save_settings(d):
    import json
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except OSError:
        pass


def convert_file(src, out, model, spot, size, font):
    W, H = page_size(model)
    with tempfile.TemporaryDirectory() as tmp:
        if not src.lower().endswith(".epub"):
            src = to_epub(src, tmp)
        title, chapters = read_epub(src)
    if not chapters:
        raise ValueError("no readable text found (is the book DRM-protected?)")
    make_pdf(title, chapters, out, W, H, tuple(spot), size, font)


# ---------------------------------------------------------------- web app
APP_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Kindle Spot Fixer</title>
<style>
 :root{--ink:#1d1d1f;--mute:#6b6b70;--line:#dcdce0;--bg:#f5f5f2;--acc:#c2410c;--ok:#15803d}
 *{box-sizing:border-box} body{margin:0;font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:var(--bg)}
 header{padding:22px 28px 6px} h1{margin:0;font-size:22px} header p{margin:4px 0 0;color:var(--mute)}
 main{display:grid;grid-template-columns:repeat(3,minmax(280px,1fr));gap:18px;padding:18px 28px 40px;align-items:start}
 @media(max-width:1000px){main{grid-template-columns:1fr}}
 section{background:#fff;border:1px solid var(--line);border-radius:12px;padding:18px}
 h2{margin:0 0 4px;font-size:16px;display:flex;gap:10px;align-items:center}
 .n{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:50%;background:var(--ink);color:#fff;font-size:13px}
 .hint{color:var(--mute);font-size:13px;margin:0 0 12px}
 select,input[type=text],input[type=number]{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:#fff}
 select{width:100%} button{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--ink);background:#fff;cursor:pointer}
 button.primary{background:var(--ink);color:#fff} button:disabled{opacity:.4;cursor:default}
 .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}
 .spec{font-size:13px;color:var(--mute);margin-top:8px}
 canvas{display:block;margin:4px auto 8px;touch-action:none;cursor:crosshair;max-width:100%}
 .cells input[type=text]{width:64px;text-transform:uppercase}
 #files{width:100%} label.chk{display:flex;gap:8px;align-items:center}
 #log{margin-top:12px;font-size:14px} #log div{padding:7px 0;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:8px}
 .ok{color:var(--ok)} .err{color:#b91c1c} .busy{color:var(--mute)} a{color:var(--acc)}
 .pill{font-size:12px;padding:2px 8px;border-radius:99px;background:#eef6ef;color:var(--ok)} .pill.no{background:#f3f3f3;color:var(--mute)}
</style></head><body>
<header><h1>Kindle Spot Fixer</h1>
<p>Lays your ebooks out so the text flows around the damaged part of the screen.</p></header>
<main>
<section>
 <h2><span class="n">1</span>Your Kindle</h2>
 <p class="hint">Not sure? On the Kindle: Settings → Device Options → Device Info.</p>
 <select id="model"></select>
 <div class="spec" id="spec"></div>
</section>
<section>
 <h2><span class="n">2</span>Mark the damaged area</h2>
 <p class="hint">Drag a box over the spot. Drag inside it to move, or drag a corner to resize. Leave a little extra room around the spot.</p>
 <canvas id="cv"></canvas>
 <div class="row cells">Grid cells <input type="text" id="c1" maxlength="3"> to <input type="text" id="c2" maxlength="3">
  <label class="chk" style="margin-left:auto"><input type="checkbox" id="prev" checked> text preview</label></div>
 <div class="row"><button id="calib">Download test grid</button>
 <span class="hint" style="margin:0">Open it on the Kindle and type in the cells the spot covers, for the most exact position.</span></div>
</section>
<section>
 <h2><span class="n">3</span>Convert books</h2>
 <p class="hint">EPUB works directly. MOBI / AZW3 need Calibre installed. DRM-free books only.</p>
 <input type="file" id="files" multiple accept=".epub,.mobi,.azw3,.azw,.prc">
 <div class="row">Text size <input type="range" id="size" min="8" max="16" step="0.5"> <b id="sizev"></b> pt</div>
 <div class="row"><label class="chk"><input type="checkbox" id="tok"> Copy straight onto the Kindle</label>
  <span id="kst" class="pill no">not found</span><button id="rechk" style="padding:3px 10px;font-size:13px">re-check</button></div>
 <button class="primary" id="go" disabled>Convert</button>
 <div class="spec" id="outdir"></div>
 <div id="log"></div>
</section>
</main>
<script>
const $=id=>document.getElementById(id);
let S={}, MODELS={}, spot=[.7,.74,.92,.87];
const N=20, cv=$('cv'), cx=cv.getContext('2d');
const L=i=>String.fromCharCode(65+i);
function cellOf(fx,fy){return L(Math.min(N-1,Math.floor(fx*N+1e-6)))+(Math.min(N-1,Math.floor(fy*N+1e-6))+1)}
function parseCell(s){const m=/^([A-Ta-t])(\d{1,2})$/.exec(s.trim());if(!m)return null;const c=m[1].toUpperCase().charCodeAt(0)-65,r=+m[2]-1;return r>=0&&r<N?[c,r]:null}
function dims(){const [w,h]=MODELS[$('model').value];const H=440,W=Math.round(H*w/h);return [W,H]}
function draw(){
 const [W,H]=dims(),pad=14,dpr=window.devicePixelRatio||1;
 cv.width=(W+2*pad)*dpr;cv.height=(H+2*pad)*dpr;cv.style.width=(W+2*pad)+'px';cv.style.height=(H+2*pad)+'px';
 cx.setTransform(dpr,0,0,dpr,0,0);
 cx.fillStyle='#2a2a2c';roundRect(0,0,W+2*pad,H+2*pad,14);cx.fill();
 cx.fillStyle='#ecebe4';cx.fillRect(pad,pad,W,H);
 cx.save();cx.translate(pad,pad);
 cx.strokeStyle='rgba(0,0,0,.07)';cx.lineWidth=1;
 for(let i=1;i<N;i++){const x=Math.round(W*i/N)+.5,y=Math.round(H*i/N)+.5;cx.beginPath();cx.moveTo(x,0);cx.lineTo(x,H);cx.moveTo(0,y);cx.lineTo(W,y);cx.stroke()}
 const [l,t,r,b]=spot;
 if($('prev').checked){ // fake text lines in the areas text will use
  const m=W*.05,lh=H/34;cx.fillStyle='rgba(0,0,0,.22)';
  for(let y=m;y<H-m-lh;y+=lh){
   const inBand=y+lh*.6>t*H&&y<b*H; let x0=m,x1=W-m;
   if(inBand){x1=Math.min(x1,l*W-4);}
   if(x1-x0>20)cx.fillRect(x0,y,x1-x0,lh*.45);
  }}
 cx.fillStyle='rgba(194,65,12,.18)';cx.strokeStyle='#c2410c';cx.lineWidth=2;cx.setLineDash([6,4]);
 cx.fillRect(l*W,t*H,(r-l)*W,(b-t)*H);cx.strokeRect(l*W,t*H,(r-l)*W,(b-t)*H);cx.setLineDash([]);
 cx.fillStyle='#c2410c';for(const [a,c] of [[l,t],[r,t],[l,b],[r,b]])cx.fillRect(a*W-4,c*H-4,8,8);
 cx.restore();
 $('c1').value=cellOf(l,t);$('c2').value=cellOf(r-1e-4,b-1e-4);
}
function roundRect(x,y,w,h,r){cx.beginPath();cx.moveTo(x+r,y);cx.arcTo(x+w,y,x+w,y+h,r);cx.arcTo(x+w,y+h,x,y+h,r);cx.arcTo(x,y+h,x,y,r);cx.arcTo(x,y,x+w,y,r);cx.closePath()}
let drag=null;
function frac(e){const [W,H]=dims(),rc=cv.getBoundingClientRect(),sx=rc.width/(W+28);return [Math.max(0,Math.min(1,((e.clientX-rc.left)/sx-14)/W)),Math.max(0,Math.min(1,((e.clientY-rc.top)/sx-14)/H))]}
cv.addEventListener('pointerdown',e=>{cv.setPointerCapture(e.pointerId);const [x,y]=frac(e),[l,t,r,b]=spot,[W,H]=dims();
 const near=(cx_,cy_)=>Math.abs((x-cx_)*W)<12&&Math.abs((y-cy_)*H)<12;
 const corners=[[l,t,r,b],[r,t,l,b],[l,b,r,t],[r,b,l,t]];
 for(const [ax,ay,ox,oy] of corners){if(near(ax,ay)){drag={mode:'new',x:ox,y:oy,s:[...spot]};return}}
 drag=(x>l&&x<r&&y>t&&y<b)?{mode:'move',x,y,s:[...spot]}:{mode:'new',x,y,s:[...spot]};});
cv.addEventListener('pointermove',e=>{if(drag)return;const [x,y]=frac(e),[l,t,r,b]=spot,[W,H]=dims();
 const nc=[[l,t],[r,t],[l,b],[r,b]].some(([a,c])=>Math.abs((x-a)*W)<12&&Math.abs((y-c)*H)<12);
 cv.style.cursor=nc?'nwse-resize':(x>l&&x<r&&y>t&&y<b)?'move':'crosshair'});
cv.addEventListener('pointermove',e=>{if(!drag)return;const [x,y]=frac(e);
 if(drag.mode==='new'){spot=[Math.min(x,drag.x),Math.min(y,drag.y),Math.max(x,drag.x),Math.max(y,drag.y)]}
 else{let [l,t,r,b]=drag.s,dx=x-drag.x,dy=y-drag.y;dx=Math.max(-l,Math.min(1-r,dx));dy=Math.max(-t,Math.min(1-b,dy));spot=[l+dx,t+dy,r+dx,b+dy]}
 draw();});
cv.addEventListener('pointerup',()=>{if(drag&&drag.mode==='new'&&(spot[2]-spot[0]<.02||spot[3]-spot[1]<.02)){spot=[...(drag.s||S.spot)]}drag=null;draw();save()});
function fromCells(){const a=parseCell($('c1').value),b=parseCell($('c2').value);if(!a||!b)return;
 spot=[Math.min(a[0],b[0])/N,Math.min(a[1],b[1])/N,(Math.max(a[0],b[0])+1)/N,(Math.max(a[1],b[1])+1)/N];draw();save()}
$('c1').addEventListener('change',fromCells);$('c2').addEventListener('change',fromCells);
$('prev').addEventListener('change',draw);
$('model').addEventListener('change',()=>{spec();draw();save()});
function spec(){const [w,h,p]=MODELS[$('model').value];$('spec').textContent=`${w} × ${h} px · ${p} ppi · ${(Math.hypot(w,h)/p).toFixed(1)}″ screen`}
$('size').addEventListener('input',()=>{$('sizev').textContent=$('size').value});
$('size').addEventListener('change',save);
function q(){return `model=${encodeURIComponent($('model').value)}&spot=${spot.map(v=>v.toFixed(4)).join(',')}&size=${$('size').value}`}
function save(){fetch('/api/settings',{method:'POST',body:JSON.stringify({model:$('model').value,spot,size:+$('size').value})})}
$('calib').onclick=()=>{location.href='/api/calibration?'+q()};
$('files').addEventListener('change',()=>{$('go').disabled=!$('files').files.length});
async function kcheck(){const r=await (await fetch('/api/kindle')).json();
 $('kst').textContent=r.kindle?'connected':'not found';$('kst').className='pill'+(r.kindle?'':' no');
 $('tok').disabled=!r.kindle;if(!r.kindle)$('tok').checked=false;else $('tok').checked=true;}
$('rechk').onclick=kcheck;
$('go').onclick=async()=>{
 $('go').disabled=true;const files=[...$('files').files];
 for(const f of files){
  const row=document.createElement('div');row.innerHTML=`<span></span><span class="busy">converting…</span>`;row.firstChild.textContent=f.name;$('log').prepend(row);
  try{const r=await fetch(`/api/convert?${q()}&name=${encodeURIComponent(f.name)}&tokindle=${$('tok').checked?1:0}`,{method:'POST',body:f});
   const j=await r.json();const st=row.lastChild;
   if(j.ok){st.className='ok';st.innerHTML=`${j.pages} pages · <a href="/out/${encodeURIComponent(j.file)}" download>download</a>`+(j.kindle?' · on Kindle ✓':'')}
   else{st.className='err';st.textContent=j.error}
  }catch(e){row.lastChild.className='err';row.lastChild.textContent='failed: '+e}
 }
 $('go').disabled=false;
};
(async()=>{const st=await (await fetch('/api/state')).json();MODELS=st.models;S=st.settings;
 for(const m of Object.keys(MODELS)){const o=document.createElement('option');o.textContent=m;$('model').append(o)}
 $('model').value=S.model;spot=S.spot;$('size').value=S.size;$('sizev').textContent=S.size;
 $('outdir').textContent='Converted books are also saved in: '+st.outdir;spec();draw();kcheck();
 window.addEventListener('resize',draw);})();
</script></body></html>"""


def run_app(port=8765, open_browser=True):
    import json, threading, webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs, unquote

    font = register_font()
    os.makedirs(OUT_DIR, exist_ok=True)
    lock = threading.Lock()   # reportlab font state isn't thread-safe

    def params(qs):
        model = qs.get("model", [DEFAULT_MODEL])[0]
        if model not in MODEL_SPECS:
            model = DEFAULT_MODEL
        try:
            spot = [min(1, max(0, float(v))) for v in qs["spot"][0].split(",")][:4]
            assert len(spot) == 4 and spot[2] > spot[0] and spot[3] > spot[1]
        except Exception:
            spot = list(DEFAULT_SPOT)
        try:
            size = min(24, max(6, float(qs.get("size", ["10.5"])[0])))
        except ValueError:
            size = 10.5
        return model, spot, size

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if u.path == "/":
                return self.send(200, APP_HTML, "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self.send(200, {"models": MODEL_SPECS,
                                       "settings": load_settings(),
                                       "outdir": OUT_DIR})
            if u.path == "/api/kindle":
                return self.send(200, {"kindle": find_kindle()})
            if u.path == "/api/calibration":
                model, spot, _ = params(qs)
                W, Hh = page_size(model)
                with tempfile.TemporaryDirectory() as tmp:
                    p = os.path.join(tmp, "c.pdf")
                    with lock:
                        make_calibration(p, W, Hh, spot, font)
                    data = open(p, "rb").read()
                return self.send(200, data, "application/pdf", {
                    "Content-Disposition": 'attachment; filename="kindle_test_grid.pdf"'})
            if u.path.startswith("/out/"):
                name = os.path.basename(unquote(u.path[5:]))
                p = os.path.join(OUT_DIR, name)
                if os.path.isfile(p):
                    return self.send(200, open(p, "rb").read(), "application/pdf", {
                        "Content-Disposition": f'attachment; filename="{name.encode("ascii", "replace").decode()}"'})
            self.send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)
            if u.path == "/api/settings":
                try:
                    d = json.loads(body)
                    save_settings({"model": d["model"], "spot": d["spot"],
                                   "size": d["size"]})
                except Exception:
                    pass
                return self.send(200, {"ok": True})
            if u.path == "/api/convert":
                model, spot, size = params(qs)
                name = os.path.basename(qs.get("name", ["book.epub"])[0]) or "book.epub"
                stem, ext = os.path.splitext(name)
                out_name = stem + "_spot.pdf"
                out = os.path.join(OUT_DIR, out_name)
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        src = os.path.join(tmp, "in" + ext.lower())
                        with open(src, "wb") as f:
                            f.write(body)
                        with lock:
                            convert_file(src, out, model, spot, size, font)
                    pages = len(re.findall(rb"/Type\s*/Page[^s]", open(out, "rb").read()))
                    on_kindle = None
                    if qs.get("tokindle", ["0"])[0] == "1":
                        docs = find_kindle()
                        if docs:
                            shutil.copy(out, os.path.join(docs, out_name))
                            on_kindle = docs
                    return self.send(200, {"ok": True, "file": out_name,
                                           "pages": pages, "kindle": on_kindle})
                except SystemExit as e:
                    return self.send(200, {"ok": False, "error": str(e)})
                except Exception as e:
                    return self.send(200, {"ok": False,
                                           "error": f"{type(e).__name__}: {e}"})
            self.send(404, {"error": "not found"})

    srv = None
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), H)
            break
        except OSError:
            continue
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Kindle Spot Fixer is running at {url}\nKeep this window open; "
          "press Ctrl+C to quit.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("books", nargs="*", help="convert without the app (uses saved settings)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if not a.books:
        return run_app(a.port, not a.no_browser)
    s = load_settings()
    font = register_font()
    for b in a.books:
        out = os.path.splitext(b)[0] + "_spot.pdf"
        convert_file(b, out, s["model"], s["spot"], s["size"], font)
        print("Wrote", out)


if __name__ == "__main__":
    main()
