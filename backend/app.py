from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from PIL import Image
import numpy as np
import os
import uuid
import glob
import json
import hashlib
import geopandas as gpd
from shapely.geometry import mapping
from werkzeug.utils import secure_filename
import rasterio
import rasterio.windows
import rasterio.enums
import rasterio.features
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import LabelEncoder
from scipy.ndimage import generic_filter
from scipy.stats import mode as scipy_mode

app = Flask(__name__)
CORS(app)

# Project layout: app.py lives in backend/, while index.html and frontend/
# sit one level up, at the project root.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@app.route('/')
def serve_index():
    return send_file(os.path.join(PROJECT_ROOT, 'index.html'))


@app.route('/frontend/<path:filename>')
def serve_frontend(filename):
    return send_file(os.path.join(PROJECT_ROOT, 'frontend', filename))

UPLOAD_FOLDER    = 'uploads'
CONVERTED_FOLDER = 'converted'
os.makedirs(UPLOAD_FOLDER,    exist_ok=True)
os.makedirs(CONVERTED_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER']      = UPLOAD_FOLDER
app.config['CONVERTED_FOLDER']   = CONVERTED_FOLDER
# Raised to comfortably fit large multi-band GeoTIFFs (drone/satellite scenes
# can easily run several hundred MB to a few GB). Override with the
# MAX_UPLOAD_MB env var if you need to go even higher.
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_UPLOAD_MB', 2048)) * 1024 * 1024

ALLOWED_EXTENSIONS = {'.tif', '.tiff', '.img', '.hdf', '.jpg', '.jpeg', '.png'}

# Above this pixel count, raster reads/writes switch to a windowed (tiled)
# strategy instead of loading the whole array into memory at once. Keeps big
# .tif uploads (IAP + NDVI) from blowing up RAM.
LARGE_IMAGE_PIXEL_THRESHOLD = 4000 * 4000
# Tile edge length (pixels) used for windowed processing. Bumped up from
# 1024 -> 2048: predict_proba has fixed per-call overhead (thread pool
# dispatch across n_jobs), so fewer/larger calls over the same total pixel
# count finish noticeably faster. 2048x2048 float32 features for 9 bands
# is still only ~150MB per tile, well within normal memory budgets.
BLOCK_SIZE = 2048
# Preview PNGs are downsampled to this max edge so huge scenes don't produce
# huge PNGs / blow up PIL. Full-resolution data is always preserved in the
# downloadable GeoTIFF.
PREVIEW_MAX_EDGE = 2500

# Training polygons — sit alongside app.py in the backend folder. This one
# file already contains every class (Invasive, Katbos, Grassland, Agriculture,
# Water, Bare Soil) — Shrub/Dekgras were folded into Grassland and Dirt road
# into Bare Soil when this file was built, so no merging happens at request
# time any more.
GEOJSON_PATH = os.path.join(os.path.dirname(__file__), 'training_data.geojson')

# Seed images — rasters bundled with the deployment that the cross-image
# fallback can always borrow training pixels from, even on a completely
# fresh install with nothing yet in uploads/. Place your original training
# images (the ones the polygons were digitized against) in this folder.
# The folder is searched AFTER uploads/ so locally-uploaded images always
# take priority as pixel sources. Seed images are never served to the user
# and are never deleted by the app.
SEED_IMAGES_DIR = os.path.join(os.path.dirname(__file__), 'seed_images')
os.makedirs(SEED_IMAGES_DIR, exist_ok=True)
CLASS_FIELD  = 'class_name'

# Fallback band layout, only used when a file has no usable per-band
# descriptions to detect roles from (see detect_band_order below).
BAND_ORDER = {'blue': 0, 'green': 1, 'red': 2, 'nir': 3}


def detect_band_order(src):
    """Figure out which raster band index is Blue/Green/Red/NIR from the
    file's own per-band descriptions (GDAL 'DESCRIPTION' metadata), instead
    of assuming a fixed Blue=0,Green=1,Red=2,NIR=3 layout.

    This matters a lot: multispectral sensors vary their band order and
    which bands they even include. For example, a MicaSense-style 5-band
    sensor commonly exports Green/Red/RedEdge/NIR/Alpha — no Blue band at
    all, and NIR isn't even in the position this app used to assume. Silently
    applying the fixed layout to a file like that reads the wrong physical
    band for every "blue"/"green"/"red" calculation (only NIR happened to
    line up by coincidence), corrupting NDVI, the RGB preview, and every
    feature the IAP classifier is trained on.

    Returns (band_roles, detected) where band_roles is a dict with keys
    'blue', 'green', 'red', 'nir' (and 'red_edge' if present), and detected
    is False if we had to fall back to the fixed default layout.
    """
    descriptions = src.descriptions or ()
    roles = {'blue': None, 'green': None, 'red': None, 'red_edge': None,
             'nir': None, 'swir': None, 'alpha': None}

    for i, desc in enumerate(descriptions):
        if not desc:
            continue
        d = desc.strip().lower()
        if 'alpha' in d or 'mask' in d:
            roles['alpha'] = i
        elif 'blue' in d and roles['blue'] is None:
            roles['blue'] = i
        elif 'swir' in d and roles['swir'] is None:
            roles['swir'] = i
        elif ('red edge' in d or 'rededge' in d or 'red_edge' in d) and roles['red_edge'] is None:
            roles['red_edge'] = i
        elif ('nir' in d or 'near infrared' in d or 'near-infrared' in d) and roles['nir'] is None:
            roles['nir'] = i
        elif 'green' in d and roles['green'] is None:
            roles['green'] = i
        elif 'red' in d and roles['red'] is None:   # checked after red-edge so it can't misfire
            roles['red'] = i

    have_core = all(roles[k] is not None for k in ('green', 'red', 'nir'))
    if not have_core:
        print(f"No usable band descriptions found (raw: {descriptions}) — "
              f"falling back to the default Blue=0,Green=1,Red=2,NIR=3 layout. "
              f"If this sensor uses a different band order, results will be wrong.")
        fallback = dict(BAND_ORDER)
        fallback.update({'swir': None, 'red_edge': None, 'alpha': None})
        return fallback, False

    if roles['blue'] is None:
        # Common on ag-focused sensors that skip Blue entirely. Green is the
        # closest available substitute — this keeps blue-dependent formulas
        # from accidentally reading NIR or Red Edge data instead.
        print(f"No Blue band in this raster (bands: {descriptions}) — "
              f"using Green (band {roles['green']}) as a stand-in for Blue.")
        roles['blue'] = roles['green']

    print(f"Detected band roles from file metadata: {roles} (raw: {descriptions})")
    return roles, True

# Class colours for PNG output (match QGIS symbology).
# Katbos uses a lighter tint of the same red family as Invasive so the two
# read as "related but distinct" in the legend.
IAP_COLORS = {
    'Invasive':    [220,  50,  50],   # vivid red
    'Katbos':      [235, 140, 140],   # light red / pink
    'Bankrot Bos': [210, 120,  20],   # burnt orange
    'Grassland':   [180, 230, 140],   # light green
    'Water':       [  0, 150, 220],   # blue
    'Agriculture': [ 40, 160,  60],   # dark green
    'Bare Soil':   [170, 130,  85],   # tan / dirt brown
}
IAP_DEFAULT_COLOR = [200, 200, 200]   # grey for unclassified pixels


# ── SPECTRAL FEATURE ENGINEERING ──────────────────────────────────────────────
# Only dimensionless ratios / indices — these are the same regardless of
# sensor, flight altitude, or calibration, so the GeoJSON-trained model
# works correctly on any uploaded image.

FEATURE_NAMES = [
    'ndvi', 'ndwi', 'ndbi', 'evi', 'savi',
    'ratio_rb', 'ratio_gn', 'ratio_rn', 'nir_norm',
]

# Feature dtype for both training and classification. float32 instead of
# float64: halves memory traffic for the feature matrix (which is what
# actually dominates runtime on big tiles — RF training/inference reads it
# repeatedly) and sklearn's tree code operates on float32 internally anyway,
# so nothing is lost by handing it float32 to start with.
FEATURE_DTYPE = np.float32


def compute_no_data_mask(blue, green, red, nir, nodata=None):
    """Flags pixels that are outside the actual flight footprint / have no
    real data, so they get masked white instead of being fed to the
    classifier (where near-zero/background pixels tend to land in whatever
    class has the darkest average reflectance — usually Bare Soil, which is
    what shows up as a solid-colored background instead of a clean mask).
    A pixel is 'no data' if:
      - every band is exactly zero (this app's original padding convention), or
      - every band is within a tiny epsilon of zero (covers near-zero
        compression/resampling noise at footprint edges that isn't exactly
        zero but isn't real data either), or
      - any band equals the raster's own declared nodata value (e.g. -10000
        on some reflectance products — reading this from the file's own
        metadata is what catches sentinels that aren't 0 or near-0 at all).
    """
    b = blue.flatten(); g = green.flatten(); r = red.flatten(); n = nir.flatten()
    mask = (b == 0) & (g == 0) & (r == 0) & (n == 0)
    eps = 1e-6
    mask |= (np.abs(b) < eps) & (np.abs(g) < eps) & (np.abs(r) < eps) & (np.abs(n) < eps)
    if nodata is not None:
        mask |= (b == nodata) | (g == nodata) | (r == nodata) | (n == nodata)
    return mask


def compute_features(blue, green, red, nir):
    eps = 1e-10
    b = blue.flatten().astype(FEATURE_DTYPE)
    g = green.flatten().astype(FEATURE_DTYPE)
    r = red.flatten().astype(FEATURE_DTYPE)
    n = nir.flatten().astype(FEATURE_DTYPE)

    ndvi     = (n - r)   / (n + r   + eps)
    ndwi     = (g - n)   / (g + n   + eps)
    ndbi     = (r - n)   / (r + n   + eps)
    evi      = 2.5 * (n - r) / (n + 6*r - 7.5*b + 1 + eps)
    savi     = 1.5 * (n - r) / (n + r + 0.5 + eps)
    ratio_rb = r / (b + eps)
    ratio_gn = g / (n + eps)
    ratio_rn = r / (n + eps)
    nir_norm = n / (n + r + g + eps)

    return np.column_stack([
        ndvi, ndwi, ndbi, evi, savi,
        ratio_rb, ratio_gn, ratio_rn, nir_norm,
    ]).astype(FEATURE_DTYPE)


# ── CONFIDENCE-THRESHOLD DEMOTION ──────────────────────────────────────────────
# If the model predicts a class but confidence is below the threshold, the
# pixel gets demoted to a safer fallback class. This is what stops whole
# agricultural fields being swallowed by the invasive class, and similarly
# keeps low-confidence Katbos calls from bleeding into open grassland.
# Raise a threshold (toward 0.75) to be more conservative about that class.
DEMOTION_RULES = {
    # predicted_class: (fallback_class, min_confidence)
    'Invasive': ('Agriculture', 0.67),
    'Katbos':   ('Grassland',   0.65),
    'Bankrot Bos': ('Grassland',   0.55),   # lowered from 0.80 (which had been
                                          # raised from the original 0.65) —
                                          # 0.80 over-corrected and started
                                          # missing real Bankrot Bos; 0.55 is
                                          # more sensitive than the original
                                          # baseline, catching more marginal
                                          # calls as Bankrot Bos rather than
                                          # demoting them to Grassland
    'Water':    ('Grassland',   0.65),
}

# Cap on how many training pixels are kept per class before fit(). Pixels
# inside the same polygon are highly spatially correlated (a big Agriculture
# polygon might contribute tens of thousands of near-identical pixels), so
# once a class has plenty of samples, more polygons mostly just slow down
# training without adding real signal. As more polygons get added to the
# GeoJSON over time, this keeps rf.fit() fast instead of scaling linearly
# with total polygon area. Raise this if a class visibly under-performs and
# you want it to see more of its available pixels.
MAX_TRAIN_SAMPLES_PER_CLASS = 8000

# Classes that must NEVER be filled in via the cross-image fallback in
# extract_training_samples, even if they have zero local training pixels on
# the current image. Add a class here once you've confirmed (as with
# Bankrot Bos) that it's spectrally close enough to some other lookalike
# that borrowing another image's samples produces confident-but-wrong
# predictions rather than just uncertain ones — raising the confidence
# threshold doesn't fix that, since the model isn't uncertain, it's
# consistently mistaking the lookalike for the real thing. For these
# classes, no local training coverage on an image means the class simply
# won't be predicted there at all, which is the safer failure mode.
NO_CROSS_IMAGE_BORROW = {
    'Bankrot Bos',   # confirmed too spectrally similar to another shrub to
                      # transfer reliably — see conversation notes for the
                      # date this was disabled and why
}


def extract_training_samples(raster_path, gdf_32735, class_list, class_to_id,
                              only_classes=None):
    """Extract (X, y) training samples from ONE raster: reprojects the
    training polygons to that raster's own CRS, reads only the window
    covering them, rasterizes classes onto it, and computes features from
    THAT raster's own pixel values (using its own detected band roles, since
    different images can have different band layouts).

    Pass only_classes to restrict which classes are rasterized/extracted —
    used when pulling fallback samples for just the handful of classes a
    "primary" image is missing, without re-extracting everything else.

    Returns (X, y, per_class_counts) — X/y may be empty arrays if nothing
    in this raster overlaps the requested classes. Any raster that can't be
    opened or has no usable band roles is skipped (returns empty), not
    raised, so one bad file in the uploads folder can't break classification
    of a different, valid image.
    """
    try:
        with rasterio.open(raster_path) as src:
            if src.count < 4:
                return np.empty((0, 9), dtype=FEATURE_DTYPE), np.array([], dtype=object), {}

            band_roles, _ = detect_band_order(src)

            gdf = gdf_32735
            if only_classes is not None:
                gdf = gdf[gdf[CLASS_FIELD].isin(only_classes)]
                if len(gdf) == 0:
                    return np.empty((0, 9), dtype=FEATURE_DTYPE), np.array([], dtype=object), {}

            if gdf.crs != src.crs:
                gdf = gdf.to_crs(src.crs)

            minx, miny, maxx, maxy = gdf.total_bounds
            if not all(np.isfinite([minx, miny, maxx, maxy])):
                return np.empty((0, 9), dtype=FEATURE_DTYPE), np.array([], dtype=object), {}

            window = rasterio.windows.from_bounds(
                minx, miny, maxx, maxy, transform=src.transform,
            ).round_offsets().round_lengths()
            window = window.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
            if window.width <= 0 or window.height <= 0:
                return np.empty((0, 9), dtype=FEATURE_DTYPE), np.array([], dtype=object), {}

            window_transform = src.window_transform(window)
            window_bands = src.read(window=window).astype(FEATURE_DTYPE)

            shapes = [
                (mapping(geom), class_to_id[cls])
                for geom, cls in zip(gdf.geometry, gdf[CLASS_FIELD])
                if geom is not None and not geom.is_empty
            ]
            if not shapes:
                return np.empty((0, 9), dtype=FEATURE_DTYPE), np.array([], dtype=object), {}

            label_arr = rasterio.features.rasterize(
                shapes,
                out_shape=(int(window.height), int(window.width)),
                transform=window_transform,
                fill=-1,
                dtype=np.int32,
            )

            blue  = window_bands[band_roles['blue']]
            green = window_bands[band_roles['green']]
            red   = window_bands[band_roles['red']]
            nir   = window_bands[band_roles['nir']]

            no_data_flat   = compute_no_data_mask(blue, green, red, nir, nodata=src.nodata)
            feature_matrix = compute_features(blue, green, red, nir)
            finite_flat    = np.all(np.isfinite(feature_matrix), axis=1)
            labels_flat    = label_arr.flatten()

            valid = (labels_flat >= 0) & ~no_data_flat & finite_flat
            if not np.any(valid):
                return np.empty((0, 9), dtype=FEATURE_DTYPE), np.array([], dtype=object), {}

            X = feature_matrix[valid]
            y = np.array(class_list, dtype=object)[labels_flat[valid]]
            counts = {cls: int(np.sum(y == cls)) for cls in np.unique(y)}
            return X, y, counts
    except Exception as e:
        print(f"  Skipping {raster_path} as a training source: {e}")
        return np.empty((0, 9), dtype=FEATURE_DTYPE), np.array([], dtype=object), {}


def apply_demotion_rules(pred_encoded, max_probs, class_names, borrowed_classes=None):
    """Mutates pred_encoded in place, demoting low-confidence predictions
    per DEMOTION_RULES. Shared by both the small-image and windowed
    large-image classification paths.

    borrowed_classes: class names that had ZERO local training pixels on
    THIS image and were only learned from another image's samples (see the
    hybrid fallback in extract_training_samples). The model's confidence
    calibration for those classes can't be trusted as much here — there's
    no local ground truth to confirm it's actually seeing that class in
    this photo, not just something spectrally similar. So they get a
    meaningfully higher confidence bar before a prediction is allowed to
    stick, on top of (or instead of) their normal DEMOTION_RULES threshold.
    """
    borrowed_classes = borrowed_classes or set()
    BORROWED_THRESHOLD_BOOST = 0.15
    BORROWED_FALLBACK_DEFAULT = 'Grassland'   # used when a borrowed-only
                                               # class has no DEMOTION_RULES
                                               # entry of its own

    handled = set()
    for predicted_name, (fallback_name, threshold) in DEMOTION_RULES.items():
        if predicted_name in class_names and fallback_name in class_names:
            effective_threshold = threshold
            if predicted_name in borrowed_classes:
                effective_threshold = min(0.9, threshold + BORROWED_THRESHOLD_BOOST)
            pred_idx     = class_names.index(predicted_name)
            fallback_idx = class_names.index(fallback_name)
            uncertain    = (pred_encoded == pred_idx) & (max_probs < effective_threshold)
            if np.any(uncertain):
                pred_encoded[uncertain] = fallback_idx
                borrowed_tag = " (borrowed, no local ground truth)" if predicted_name in borrowed_classes else ""
                print(f"  Demoted {uncertain.sum():,} low-confidence {predicted_name}{borrowed_tag} → {fallback_name}")
            handled.add(predicted_name)

    # Safety net: a class can be borrowed-only on this image but have no
    # DEMOTION_RULES entry at all (e.g. Agriculture, Bare Soil normally
    # don't need one because they're usually well-represented locally).
    # Without this, a borrowed-only prediction for one of those would have
    # no confidence check applied whatsoever.
    if BORROWED_FALLBACK_DEFAULT in class_names:
        fallback_idx = class_names.index(BORROWED_FALLBACK_DEFAULT)
        for predicted_name in borrowed_classes - handled:
            if predicted_name not in class_names or predicted_name == BORROWED_FALLBACK_DEFAULT:
                continue
            pred_idx  = class_names.index(predicted_name)
            uncertain = (pred_encoded == pred_idx) & (max_probs < 0.75)
            if np.any(uncertain):
                pred_encoded[uncertain] = fallback_idx
                print(f"  Demoted {uncertain.sum():,} low-confidence borrowed-only "
                      f"{predicted_name} (no local ground truth) → {BORROWED_FALLBACK_DEFAULT}")


# ── HELPERS ────────────────────────────────────────────────────────────────────
def allowed_file(filename):
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS


def is_large_raster(src):
    return (src.width * src.height) > LARGE_IMAGE_PIXEL_THRESHOLD


def preview_out_shape(src):
    """Decimated (band_count, h, w) shape for reading a display-friendly
    preview of a large raster without pulling full resolution into memory."""
    scale = min(1.0, PREVIEW_MAX_EDGE / max(src.width, src.height))
    out_h = max(1, int(src.height * scale))
    out_w = max(1, int(src.width  * scale))
    return src.count, out_h, out_w


def convert_multispectral_to_rgb(input_path, output_path):
    try:
        with rasterio.open(input_path) as src:
            if is_large_raster(src):
                # Read a decimated overview instead of the full array —
                # keeps memory bounded regardless of source file size.
                count, out_h, out_w = preview_out_shape(src)
                bands = src.read(
                    out_shape=(count, out_h, out_w),
                    resampling=rasterio.enums.Resampling.average,
                )
            else:
                bands = src.read()

            n_bands = bands.shape[0]
            if n_bands >= 4:
                band_roles, _ = detect_band_order(src)
                r = bands[band_roles['red']].astype(float)
                g = bands[band_roles['green']].astype(float)
                b = bands[band_roles['blue']].astype(float)
            elif n_bands == 3:
                r, g, b = bands[0].astype(float), bands[1].astype(float), bands[2].astype(float)
            else:
                gray = bands[0].astype(float)
                r = g = b = gray

            # Exclude nodata sentinel pixels (e.g. -10000 on reflectance
            # products, or the all-zero convention used elsewhere in this
            # app) from the stretch — including them was blowing out the
            # min/max range and produced near-black or oversaturated previews.
            no_data_mask = (r == 0) & (g == 0) & (b == 0)
            if src.nodata is not None:
                no_data_mask |= (r == src.nodata) | (g == src.nodata) | (b == src.nodata)

            rgb_img = np.zeros((r.shape[0], r.shape[1], 3), dtype=np.uint8)
            for i, channel in enumerate((r, g, b)):
                valid_vals = channel[~no_data_mask]
                if valid_vals.size == 0:
                    continue
                lo, hi = np.percentile(valid_vals, [2, 98])
                if hi <= lo:
                    lo, hi = float(valid_vals.min()), float(valid_vals.max())
                if hi <= lo:
                    hi = lo + 1
                stretched = np.clip((channel - lo) / (hi - lo), 0, 1) * 255
                rgb_img[:, :, i] = stretched.astype(np.uint8)

            rgb_img[no_data_mask] = 0
            Image.fromarray(rgb_img).save(output_path, 'PNG')
        return True, "Conversion successful"
    except Exception as e:
        try:
            # PIL fallback also needs a size cap so a huge plain JPG/PNG
            # can't exhaust memory either.
            Image.MAX_IMAGE_PIXELS = 300_000_000
            img = Image.open(input_path)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            if max(img.size) > PREVIEW_MAX_EDGE:
                img.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE))
            img.save(output_path, 'PNG')
            return True, "Conversion successful (PIL fallback)"
        except Exception:
            return False, f"Conversion failed: {str(e)}"


# ── ROUTES ─────────────────────────────────────────────────────────────────────

@app.errorhandler(413)
def file_too_large(e):
    max_mb = app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)
    return jsonify({
        'error': f'File is too large. Maximum upload size is {max_mb} MB.'
    }), 413


@app.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    if not allowed_file(file.filename):
        return jsonify({'error': 'File type not allowed'}), 400

    original_filename = secure_filename(file.filename)
    file_id           = str(uuid.uuid4())
    ext               = os.path.splitext(original_filename)[1].lower()
    original_path     = os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}{ext}")
    file.save(original_path)

    displayable_formats = {'.jpg', '.jpeg', '.png'}
    needs_conversion    = ext not in displayable_formats
    converted_url       = None

    if needs_conversion:
        converted_filename = f"{file_id}.png"
        converted_path     = os.path.join(app.config['CONVERTED_FOLDER'], converted_filename)
        success, message   = convert_multispectral_to_rgb(original_path, converted_path)
        if success:
            converted_url = f"/api/converted/{converted_filename}"
        else:
            return jsonify({'error': message, 'filename': original_filename,
                            'needs_conversion': True}), 500

    return jsonify({
        'success':          True,
        'filename':         original_filename,
        'file_id':          file_id,
        'original_url':     f"/api/uploads/{file_id}{ext}" if not needs_conversion else None,
        'converted_url':    converted_url,
        'file_size':        os.path.getsize(original_path),
        'needs_conversion': needs_conversion,
        'message':          'File uploaded successfully',
    })


@app.route('/api/uploads/<filename>')
def serve_upload(filename):
    return send_file(os.path.join(app.config['UPLOAD_FOLDER'], filename))

@app.route('/api/converted/<filename>')
def serve_converted(filename):
    return send_file(os.path.join(app.config['CONVERTED_FOLDER'], filename))

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'message': 'Server is running'})


NDVI_COLOR_STOPS = [
    (-1.0, (  0,  80, 200)),
    (-0.3, (  0, 180, 255)),
    (-0.1, ( 80, 210, 255)),
    ( 0.0, (255, 220, 100)),
    ( 0.2, (255, 160,   0)),
    ( 0.4, (220,  80,   0)),
    ( 0.6, (180,  20,  20)),
    ( 0.8, (120,   0,   0)),
    ( 1.0, ( 60,   0,   0)),
]


def compute_ndvi_and_water(blue, green, red, nir, swir=None, nir_p95=None):
    """Shared NDVI + water-mask math used for both full-array (small image)
    and per-window (large image) processing."""
    no_data_mask = (blue == 0) & (green == 0) & (red == 0) & (nir == 0)

    ndvi = (nir - red) / (nir + red + 1e-10)
    ndvi = np.clip(ndvi, -1, 1)
    ndvi[no_data_mask] = np.nan

    if swir is not None:
        mndwi          = (green - swir) / (green + swir + 1e-10)
        water_spectral = mndwi > 0.0
    else:
        ndwi           = (green - nir) / (green + nir + 1e-10)
        water_spectral = ndwi > 0.0

    if nir_p95 is None:
        valid_nir = nir[~no_data_mask & (nir > 0)]
        nir_p95   = np.percentile(valid_nir, 95) if valid_nir.size else 1.0

    water_low_nir = (nir < nir_p95 * 0.25) & (nir > 0)
    water_blue    = (blue > nir) & (blue > 0)
    water_ndvi    = (~np.isnan(ndvi)) & (ndvi < -0.05) & (green > 0)

    signal_count = (water_spectral.astype(int) + water_low_nir.astype(int) +
                    water_blue.astype(int)     + water_ndvi.astype(int))
    water_mask = (signal_count >= 2) & ~no_data_mask
    return ndvi, water_mask, no_data_mask, nir_p95


def ndvi_to_rgb(ndvi, water_mask):
    rgb        = np.zeros((ndvi.shape[0], ndvi.shape[1], 3), dtype=np.uint8)
    valid_mask = ~np.isnan(ndvi)

    for i in range(len(NDVI_COLOR_STOPS) - 1):
        ndvi_low, color_low   = NDVI_COLOR_STOPS[i]
        ndvi_high, color_high = NDVI_COLOR_STOPS[i + 1]
        in_range = valid_mask & (ndvi >= ndvi_low) & (ndvi < ndvi_high)
        if np.any(in_range):
            t = np.clip((ndvi[in_range] - ndvi_low) / (ndvi_high - ndvi_low), 0, 1)
            for ch in range(3):
                rgb[in_range, ch] = (
                    color_low[ch] + (color_high[ch] - color_low[ch]) * t
                ).astype(np.uint8)

    rgb[water_mask]  = [0, 180, 255]
    rgb[~valid_mask] = [255, 255, 255]
    return rgb


# ── NDVI ───────────────────────────────────────────────────────────────────────
@app.route('/api/ndvi', methods=['POST'])
def calculate_ndvi():
    data    = request.get_json()
    file_id = data.get('file_id')
    if not file_id:
        return jsonify({'error': 'No file_id provided'}), 400

    files = glob.glob(os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.*"))
    if not files:
        return jsonify({'error': 'Original file not found'}), 404
    input_path = files[0]

    try:
        with rasterio.open(input_path) as src:
            if src.count < 4:
                return jsonify({'error': 'Image needs at least 4 bands for NDVI'}), 400

            band_roles, _ = detect_band_order(src)
            bi = lambda key: band_roles[key]  # short alias below

            ndvi_tif_filename = f"{file_id}_ndvi.tif"
            ndvi_tif_path     = os.path.join(app.config['CONVERTED_FOLDER'], ndvi_tif_filename)
            ndvi_png_filename = f"{file_id}_ndvi.png"
            ndvi_png_path     = os.path.join(app.config['CONVERTED_FOLDER'], ndvi_png_filename)

            profile = src.profile
            profile.update(dtype=rasterio.float32, count=1, compress='lzw', nodata=np.nan)

            if is_large_raster(src):
                # ── Large image: tile through the full-res data window by
                # window so we never hold the whole raster in memory. ──────
                count, out_h, out_w = preview_out_shape(src)
                preview_bands = src.read(
                    out_shape=(count, out_h, out_w),
                    resampling=rasterio.enums.Resampling.average,
                ).astype(float)
                swir_p = preview_bands[bi('swir')] if band_roles['swir'] is not None else None
                p_ndvi, p_water, _, nir_p95 = compute_ndvi_and_water(
                    preview_bands[bi('blue')], preview_bands[bi('green')],
                    preview_bands[bi('red')], preview_bands[bi('nir')],
                    swir=swir_p,
                )
                Image.fromarray(ndvi_to_rgb(p_ndvi, p_water)).save(ndvi_png_path, 'PNG')

                with rasterio.open(ndvi_tif_path, 'w', **profile) as dst:
                    # Iterate our own fixed-size tile grid (rather than the
                    # source file's internal block layout) for predictable,
                    # bounded memory use regardless of input tiling.
                    for row_off in range(0, src.height, BLOCK_SIZE):
                        for col_off in range(0, src.width, BLOCK_SIZE):
                            win = rasterio.windows.Window(
                                col_off, row_off,
                                min(BLOCK_SIZE, src.width - col_off),
                                min(BLOCK_SIZE, src.height - row_off),
                            )
                            block = src.read(window=win).astype(float)
                            swir_b = block[bi('swir')] if band_roles['swir'] is not None else None
                            b_ndvi, _, _, _ = compute_ndvi_and_water(
                                block[bi('blue')], block[bi('green')],
                                block[bi('red')], block[bi('nir')],
                                swir=swir_b, nir_p95=nir_p95,
                            )
                            dst.write(b_ndvi.astype(rasterio.float32), 1, window=win)
            else:
                # ── Small/medium image: original single-pass approach. ─────
                bands = src.read().astype(float)
                swir  = bands[bi('swir')] if band_roles['swir'] is not None else None
                ndvi, water_mask, _, _ = compute_ndvi_and_water(
                    bands[bi('blue')], bands[bi('green')], bands[bi('red')], bands[bi('nir')],
                    swir=swir,
                )
                with rasterio.open(ndvi_tif_path, 'w', **profile) as dst:
                    dst.write(ndvi.astype(rasterio.float32), 1)
                Image.fromarray(ndvi_to_rgb(ndvi, water_mask)).save(ndvi_png_path, 'PNG')

        return jsonify({
            'success':      True,
            'preview_url':  f"/api/converted/{ndvi_png_filename}",
            'download_url': f"/api/converted/{ndvi_tif_filename}",
            'message':      'NDVI calculated successfully',
        })
    except Exception as e:
        print(f"NDVI Error: {str(e)}")
        return jsonify({'error': str(e)}), 500


import joblib

MODEL_DIR = os.path.dirname(__file__)


def model_path_for(file_id):
    """Each uploaded raster gets its own cached model file, keyed by
    file_id. A model is only ever trained on pixels from the specific image
    it will classify, so it must never be reused across different images —
    even if training_data.geojson hasn't changed. (Two images can overlap
    completely different subsets of the training polygons — e.g. one
    covering the Katbos survey area and another not — so a model "valid" for
    one image's footprint can be silently wrong for another's.)"""
    safe_id = "".join(c for c in file_id if c.isalnum() or c in "-_")
    return os.path.join(MODEL_DIR, f'iap_model_{safe_id}.pkl')


def training_data_fingerprint():
    """MD5 of the training GeoJSON's contents. Stored alongside the cached
    model so that editing training_data.geojson (adding a class, merging
    classes, adding more polygons, ...) automatically invalidates the old
    cached model instead of silently continuing to serve stale predictions
    that don't know about the new data."""
    if not os.path.exists(GEOJSON_PATH):
        return None
    with open(GEOJSON_PATH, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()


@app.route('/api/iap', methods=['POST'])
def classify_iap():
    from scipy.ndimage import convolve

    data    = request.get_json()
    file_id = data.get('file_id')
    if not file_id:
        return jsonify({'error': 'No file_id provided'}), 400

    files = glob.glob(os.path.join(app.config['UPLOAD_FOLDER'], f"{file_id}.*"))
    if not files:
        return jsonify({'error': 'Uploaded file not found'}), 404
    input_path = files[0]

    try:
        with rasterio.open(input_path) as src:
            raster_crs = src.crs
            n_bands, height, width = src.count, src.height, src.width
            if n_bands < 4:
                return jsonify({'error': 'Image needs at least 4 bands (Blue, Green, Red, NIR)'}), 400

            band_roles, _ = detect_band_order(src)
            model_path = model_path_for(file_id)

            # ─ PATH A: PRE-TRAINED MODEL EXISTS AND MATCHES CURRENT DATA ──
            current_fingerprint = training_data_fingerprint()
            cached_model_is_fresh = False
            if os.path.exists(model_path):
                print(f"Loading pre-trained model from {model_path}...")
                try:
                    with open(model_path, 'rb') as f:
                        payload = joblib.load(f)
                    if payload.get('training_fingerprint') == current_fingerprint:
                        rf = payload['model']
                        class_names = payload['class_names']
                        class_list  = payload.get('class_list', class_names)
                        training_sample_counts = payload.get('training_sample_counts', {})
                        borrowed_from = payload.get('borrowed_from', {})
                        cached_model_is_fresh = True
                        print(f"Model loaded. Classes: {class_names}")
                    else:
                        print("training_data.geojson has changed since this model was "
                              "trained — retraining instead of using the stale cache.")
                except Exception as e:
                    print(f"Cached model failed to load ({e}) — retraining instead.")

            # ── PATH B: NO MODEL YET, OR IT'S STALE — TRAIN FROM GEOJSON ────
            if not cached_model_is_fresh:
                print("Training from GeoJSON...")
                if not os.path.exists(GEOJSON_PATH):
                    return jsonify({'error': f'Training polygons not found at {GEOJSON_PATH}'}), 500

                try:
                    gdf = gpd.read_file(GEOJSON_PATH, engine='pyogrio')
                except Exception:
                    gdf = gpd.read_file(GEOJSON_PATH)
                gdf = gdf[gdf[CLASS_FIELD].notna()].copy()

                # training_data.geojson's coordinates are written in
                # EPSG:32735 (UTM 35S) — declared via the legacy top-level
                # "crs" member. Some GeoJSON readers (depending on the
                # GDAL/pyogrio/fiona version in play) ignore that member
                # entirely and silently assume WGS84 lon/lat instead. If that
                # happens here, gdf.crs comes back as EPSG:4326 while the
                # actual numbers are still UTM meters (e.g. x=436506) — values
                # that are nonsense as longitude. Reprojecting nonsense
                # coordinates through to_crs() below can then produce
                # inf/NaN bounds, which is what was crashing with "cannot
                # convert float infinity to integer" downstream. Forcing the
                # known-correct CRS here sidesteps that ambiguity entirely.
                if gdf.crs is None or gdf.crs.to_epsg() != 32735:
                    print(f"Training GeoJSON CRS read as {gdf.crs}, expected EPSG:32735 — "
                          f"forcing it explicitly rather than trusting the file's declared CRS.")
                    gdf = gdf.set_crs(epsg=32735, allow_override=True)

                if gdf.crs != raster_crs:
                    gdf = gdf.to_crs(raster_crs)

                class_list   = sorted(gdf[CLASS_FIELD].unique())
                class_to_id  = {c: i for i, c in enumerate(class_list)}

                X, y, local_counts = extract_training_samples(
                    input_path, gdf, class_list, class_to_id,
                )
                # If this image has zero local overlap with the training polygons,
                # try seed images before giving up — this makes a fresh deployment
                # work immediately without needing to upload the original images first.
                if X.shape[0] == 0:
                    print("No local training overlap — trying seed images as pixel source...")
                    seed_files = [
                        f for f in glob.glob(os.path.join(SEED_IMAGES_DIR, '*'))
                        if os.path.isfile(f)
                    ]
                    for seed_path in seed_files:
                        X_seed, y_seed, seed_counts = extract_training_samples(
                            seed_path, gdf, class_list, class_to_id,
                        )
                        if X_seed.shape[0] > 0:
                            X = X_seed
                            y = y_seed
                            local_counts = seed_counts
                            print(f"  Bootstrapped training from seed image: {os.path.basename(seed_path)}")
                            break

                if X.shape[0] == 0:
                    return jsonify({
                        'error': 'Training polygons do not overlap this image and no seed images '
                                 'are available. Add the original training rasters to '
                                 'backend/seed_images/ so any image can be classified '
                                 'on a fresh deployment.'
                    }), 400

                training_sample_counts = {cls: local_counts.get(cls, 0) for cls in class_list}
                borrowed_from = {}  # class_name -> filename samples were pulled from

                missing_classes = [
                    c for c in class_list
                    if training_sample_counts[c] == 0 and c not in NO_CROSS_IMAGE_BORROW
                ]
                blocked_classes = [
                    c for c in class_list
                    if training_sample_counts[c] == 0 and c in NO_CROSS_IMAGE_BORROW
                ]
                if blocked_classes:
                    print(f"Classes with zero local training pixels that are NOT eligible for "
                          f"cross-image borrowing (see NO_CROSS_IMAGE_BORROW): {blocked_classes} — "
                          f"these will not be predictable on this image.")
                if missing_classes:
                    print(f"Classes with zero local training pixels on this image: "
                          f"{missing_classes} — searching other uploaded images for fallback samples.")
                    # Search uploads/ first (locally uploaded images take
                    # priority as they're more likely to match this image's
                    # geography), then seed_images/ as the guaranteed fallback.
                    other_files = [
                        f for f in glob.glob(os.path.join(app.config['UPLOAD_FOLDER'], '*'))
                        if os.path.abspath(f) != os.path.abspath(input_path) and os.path.isfile(f)
                    ] + [
                        f for f in glob.glob(os.path.join(SEED_IMAGES_DIR, '*'))
                        if os.path.isfile(f)
                    ]
                    for other_path in other_files:
                        if not missing_classes:
                            break
                        X_extra, y_extra, extra_counts = extract_training_samples(
                            other_path, gdf, class_list, class_to_id,
                            only_classes=missing_classes,
                        )
                        if X_extra.shape[0] == 0:
                            continue
                        X = np.vstack([X, X_extra])
                        y = np.concatenate([y, y_extra])
                        for cls, n in extra_counts.items():
                            training_sample_counts[cls] = training_sample_counts.get(cls, 0) + n
                            borrowed_from.setdefault(cls, os.path.basename(other_path))
                        print(f"  Borrowed {dict(extra_counts)} training pixels from "
                              f"{os.path.basename(other_path)}")
                        missing_classes = [c for c in missing_classes if training_sample_counts[c] == 0]

                    if missing_classes:
                        print(f"Still no training pixels anywhere for: {missing_classes} — "
                              f"these classes will not be predictable on this image.")

                print(f"Total training pixels per class (local + borrowed): {training_sample_counts}")

                # Cap samples per class for speed
                rng      = np.random.default_rng(42)
                keep_idx = []
                for cls in class_list:
                    cls_idx = np.where(y == cls)[0]
                    if len(cls_idx) > MAX_TRAIN_SAMPLES_PER_CLASS:
                        cls_idx = rng.choice(cls_idx, MAX_TRAIN_SAMPLES_PER_CLASS, replace=False)
                    keep_idx.append(cls_idx)
                keep_idx = np.concatenate(keep_idx)
                X = X[keep_idx]
                y = y[keep_idx]

                le          = LabelEncoder()
                y_enc       = le.fit_transform(y)
                class_names = list(le.classes_)

                CLASS_WEIGHTS = {
                    'Water': 1.0, 'Invasive': 1.0, 'Katbos': 1.0, 'Bankrot Bos': 1.0,
                    'Agriculture': 1.15, 'Grassland': 1.0, 'Bare Soil': 1.3,
                }
                custom_weights = {i: CLASS_WEIGHTS.get(name, 1.0) for i, name in enumerate(class_names)}

                rf = RandomForestClassifier(
                    n_estimators=90, max_depth=11, min_samples_leaf=30,
                    min_samples_split=60, max_samples=0.6, n_jobs=-1,
                    random_state=42, class_weight=custom_weights,
                )
                rf.fit(X, y_enc)
                print(f"Model trained. Classes: {class_names}")

                # SAVE THE MODEL FOR FUTURE USE
                payload = {
                    'model': rf,
                    'class_names': class_names,
                    'class_list': class_list,
                    'training_fingerprint': current_fingerprint,
                    'training_sample_counts': training_sample_counts,
                    'borrowed_from': borrowed_from,
                }
                with open(model_path, 'wb') as f:
                    joblib.dump(payload, f)
                print(f"Model saved to {model_path}")

            # ── CLASSIFICATION LOGIC (Shared for both paths) ────────────────
            # Classes with zero local training coverage on this specific
            # image (only learned via the cross-image fallback in
            # extract_training_samples) — passed to apply_demotion_rules so
            # it can apply a stricter confidence bar to them.
            borrowed_classes = set(borrowed_from.keys())

            numeric_2d = np.full((height, width), 255, dtype=np.uint8)
            prob_2d    = np.zeros((height, width), dtype=np.float32)
            no_data_2d = np.zeros((height, width), dtype=bool)

            if is_large_raster(src):
                for row_off in range(0, height, BLOCK_SIZE):
                    for col_off in range(0, width, BLOCK_SIZE):
                        win = rasterio.windows.Window(
                            col_off, row_off,
                            min(BLOCK_SIZE, width  - col_off),
                            min(BLOCK_SIZE, height - row_off),
                        )
                        block = src.read(window=win).astype(FEATURE_DTYPE)
                        blue  = block[band_roles['blue']]
                        green = block[band_roles['green']]
                        red   = block[band_roles['red']]
                        nir   = block[band_roles['nir']]

                        no_data_block = compute_no_data_mask(blue, green, red, nir, nodata=src.nodata)
                        feats       = compute_features(blue, green, red, nir)
                        # Pixels with a non-finite feature (e.g. a 0/0 ratio)
                        # are also treated as no-data for display purposes —
                        # otherwise they're left uncoded and show up as a
                        # stray default-grey speckle instead of a clean mask.
                        no_data_block = no_data_block | ~np.all(np.isfinite(feats), axis=1)
                        valid_block = ~no_data_block

                        pred_block = np.full(blue.size, 255, dtype=np.uint8)
                        prob_block = np.zeros(blue.size, dtype=np.float32)
                        if np.any(valid_block):
                            probs             = rf.predict_proba(feats[valid_block])
                            pred_valid        = np.argmax(probs, axis=1).astype(np.uint8)
                            max_probs_valid   = np.max(probs, axis=1).astype(np.float32)
                            apply_demotion_rules(pred_valid, max_probs_valid, class_names, borrowed_classes)
                            pred_block[valid_block] = pred_valid
                            prob_block[valid_block] = max_probs_valid

                        r0, c0, h, w = win.row_off, win.col_off, win.height, win.width
                        numeric_2d[r0:r0+h, c0:c0+w] = pred_block.reshape(h, w)
                        prob_2d[r0:r0+h, c0:c0+w]    = prob_block.reshape(h, w)
                        no_data_2d[r0:r0+h, c0:c0+w] = no_data_block.reshape(h, w)
            else:
                bands = src.read().astype(FEATURE_DTYPE)
                blue  = bands[band_roles['blue']]
                green = bands[band_roles['green']]
                red   = bands[band_roles['red']]
                nir   = bands[band_roles['nir']]

                no_data_flat = compute_no_data_mask(blue, green, red, nir, nodata=src.nodata)
                feature_matrix = compute_features(blue, green, red, nir)
                no_data_flat   = no_data_flat | ~np.all(np.isfinite(feature_matrix), axis=1)
                valid_mask     = ~no_data_flat
                valid_features = feature_matrix[valid_mask]

                probabilities = rf.predict_proba(valid_features)
                pred_encoded  = np.argmax(probabilities, axis=1).astype(np.uint8)
                max_probs     = np.max(probabilities, axis=1).astype(np.float32)
                apply_demotion_rules(pred_encoded, max_probs, class_names, borrowed_classes)

                numeric_2d.reshape(-1)[valid_mask] = pred_encoded
                no_data_2d = no_data_flat.reshape(height, width)
                prob_2d.reshape(-1)[valid_mask] = max_probs

            # ── BANKROT BOS SPATIAL CONSTRAINT ────────────────────────────────
            # The model was trained on Bankrot Bos polygons drawn in a specific
            # area. Outside those polygons it can confuse shadows with Bankrot Bos.
            #
            # This block detects whether the uploaded image actually overlaps the
            # Bankrot Bos training polygon area:
            #
            # CASE A — image OVERLAPS training polygons (your survey area):
            #   - Inside polygons  → always show Bankrot Bos (ground truth)
            #   - Outside polygons → only show if confidence >= BANKROT_OUTSIDE_THRESHOLD
            #
            # CASE B — image does NOT overlap (different farm/area entirely):
            #   - No spatial constraint applied
            #   - Falls back to standard confidence threshold from DEMOTION_RULES
            #   - Bankrot Bos can still be predicted if model is confident enough
            #
            # Tuning knobs:
            #   BANKROT_OUTSIDE_THRESHOLD (0.92): raise → only show where drawn,
            #     lower → allow more detections outside drawn polygons
            #   inside force-paint threshold (0.40): lower → paint more of the
            #     polygon interior, raise → only paint high-confidence interior px
            BANKROT_OUTSIDE_THRESHOLD  = 0.92
            BANKROT_INSIDE_MIN_CONF    = 0.40

            if 'Bankrot Bos' in class_names:
                bb_idx  = class_names.index('Bankrot Bos')
                grs_idx = class_names.index('Grassland') if 'Grassland' in class_names else 0

                try:
                    bb_polys = gdf[gdf[CLASS_FIELD] == 'Bankrot Bos']

                    # Check if the training polygons actually overlap this image
                    # by comparing bounding boxes in the raster CRS.
                    image_bounds = src.bounds  # (left, bottom, right, top)
                    if len(bb_polys) > 0:
                        poly_bounds = bb_polys.total_bounds  # (minx, miny, maxx, maxy)
                        overlaps = (
                            poly_bounds[0] < image_bounds.right  and
                            poly_bounds[2] > image_bounds.left   and
                            poly_bounds[1] < image_bounds.top    and
                            poly_bounds[3] > image_bounds.bottom
                        )
                    else:
                        overlaps = False

                    if overlaps:
                        # CASE A: image covers the Bankrot Bos survey area
                        print("  Bankrot Bos spatial constraint: polygon zone overlaps image — applying constraint")
                        bb_shapes = [
                            (mapping(geom), 1)
                            for geom in bb_polys.geometry
                            if geom is not None and not geom.is_empty
                        ]
                        bankrot_zone = rasterio.features.rasterize(
                            bb_shapes,
                            out_shape=(height, width),
                            transform=src.transform,
                            fill=0,
                            dtype=np.uint8,
                        ).astype(bool)

                        # Inside polygon zone → force-paint as Bankrot Bos
                        # for any pixel with enough confidence
                        inside_confident = (
                            bankrot_zone &
                            (prob_2d >= BANKROT_INSIDE_MIN_CONF) &
                            ~no_data_2d
                        )
                        numeric_2d[inside_confident] = bb_idx

                        # Outside polygon zone → very high confidence required
                        outside_zone = ~bankrot_zone & ~no_data_2d
                        low_conf_outside = (
                            outside_zone &
                            (numeric_2d == bb_idx) &
                            (prob_2d < BANKROT_OUTSIDE_THRESHOLD)
                        )
                        numeric_2d[low_conf_outside] = grs_idx
                        print(f"    {bankrot_zone.sum():,} px in polygon zone, "
                              f"{low_conf_outside.sum():,} px outside demoted → Grassland")
                    else:
                        # CASE B: completely different image — no spatial constraint
                        # Standard DEMOTION_RULES threshold already applied above
                        print("  Bankrot Bos spatial constraint: polygon zone does NOT overlap "
                              "this image — skipping constraint, using standard confidence threshold")

                except Exception as e:
                    print(f"  Bankrot Bos spatial constraint failed (non-fatal): {e}")

            # ── SMOOTHING & VISUALIZATION (Same as before) ──────────────────
            unique_classes = np.unique(numeric_2d[numeric_2d != 255])
            if len(unique_classes) > 0:
                kernel      = np.ones((3, 3), dtype=np.float32)
                best_count  = np.full((height, width), -1.0, dtype=np.float32)
                best_class  = numeric_2d.copy()
                for c in unique_classes:
                    count_c = convolve((numeric_2d == c).astype(np.float32), kernel, mode='nearest')
                    take    = count_c > best_count
                    best_count[take] = count_c[take]
                    best_class[take] = c
                smoothed = best_class.astype(np.uint8)
                smoothed[no_data_2d] = 255
            else:
                smoothed = numeric_2d.copy()

            color_definitions = {
                'Invasive':    {'base': [200,   0,   0], 'pale': [255, 220, 220]},
                'Katbos':      {'base': [225, 120, 120], 'pale': [255, 235, 235]},
                'Bankrot Bos': {'base': [195, 110,  15], 'pale': [255, 225, 180]},
                'Grassland':   {'base': [100, 180,  80], 'pale': [230, 245, 220]},
                'Water':       {'base': [  0, 100, 200], 'pale': [220, 240, 255]},
                'Agriculture': {'base': [ 20, 120,  40], 'pale': [220, 240, 220]},
                'Bare Soil':   {'base': [150, 110,  65], 'pale': [235, 220, 195]},
            }

            rgb = np.full((height, width, 3), IAP_DEFAULT_COLOR, dtype=np.uint8)
            for i, name in enumerate(class_names):
                if name not in color_definitions: continue
                mask = (smoothed == i)
                if not np.any(mask): continue
                base = np.array(color_definitions[name]['base'])
                pale = np.array(color_definitions[name]['pale'])
                p    = np.clip(prob_2d[mask], 0.3, 1.0)[:, np.newaxis]
                rgb[mask] = (base * p + pale * (1 - p)).astype(np.uint8)

            rgb[no_data_2d] = [255, 255, 255]

            iap_png_filename = f"{file_id}_iap.png"
            iap_png_path     = os.path.join(app.config['CONVERTED_FOLDER'], iap_png_filename)
            preview_img = Image.fromarray(rgb)
            if max(height, width) > PREVIEW_MAX_EDGE:
                preview_img.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), Image.NEAREST)
            preview_img.save(iap_png_path, 'PNG')

            iap_tif_filename = f"{file_id}_iap.tif"
            iap_tif_path     = os.path.join(app.config['CONVERTED_FOLDER'], iap_tif_filename)
            rgb_profile = src.profile
            rgb_profile.update(dtype=rasterio.uint8, count=3, compress='lzw', nodata=0)
            
            if is_large_raster(src):
                with rasterio.open(iap_tif_path, 'w', **rgb_profile) as dst:
                    for row_off in range(0, height, BLOCK_SIZE):
                        for col_off in range(0, width, BLOCK_SIZE):
                            win = rasterio.windows.Window(
                                col_off, row_off,
                                min(BLOCK_SIZE, width  - col_off),
                                min(BLOCK_SIZE, height - row_off),
                            )
                            r0, c0 = win.row_off, win.col_off
                            h, w   = win.height, win.width
                            dst.write(rgb[r0:r0+h, c0:c0+w, 0], 1, window=win)
                            dst.write(rgb[r0:r0+h, c0:c0+w, 1], 2, window=win)
                            dst.write(rgb[r0:r0+h, c0:c0+w, 2], 3, window=win)
            else:
                with rasterio.open(iap_tif_path, 'w', **rgb_profile) as dst:
                    dst.write(rgb[:, :, 0], 1)
                    dst.write(rgb[:, :, 1], 2)
                    dst.write(rgb[:, :, 2], 3)

            # Classes that exist purely as an internal modeling detail (to
            # give the classifier a tighter decision boundary) but should be
            # completely invisible to the user — map them onto the
            # user-facing class they're visually/semantically part of.
            # (Currently empty — no hidden classes in play right now.)
            DISPLAY_NAME = {}

            total_valid = int(np.sum(smoothed != 255))
            class_stats = {}
            for class_name in class_names:
                idx         = class_names.index(class_name)
                count       = int(np.sum(smoothed == idx))
                display_name = DISPLAY_NAME.get(class_name, class_name)
                if display_name in class_stats:
                    class_stats[display_name]['pixels'] += count
                else:
                    class_stats[display_name] = {'pixels': count, 'percent': 0}
            for stats in class_stats.values():
                stats['percent'] = round(stats['pixels'] / total_valid * 100, 2) if total_valid else 0

            display_class_names = list(dict.fromkeys(
                DISPLAY_NAME.get(c, c) for c in class_names
            ))
            display_training_counts = {}
            for cls, n in training_sample_counts.items():
                display_name = DISPLAY_NAME.get(cls, cls)
                display_training_counts[display_name] = display_training_counts.get(display_name, 0) + n

            display_borrowed_from = {
                DISPLAY_NAME.get(cls, cls): fname for cls, fname in borrowed_from.items()
            }
            blocked_from_borrowing = [
                DISPLAY_NAME.get(cls, cls) for cls in class_list
                if training_sample_counts.get(cls, 0) == 0 and cls in NO_CROSS_IMAGE_BORROW
            ]

        return jsonify({
            'success':      True,
            'preview_url':  f"/api/converted/{iap_png_filename}",
            'download_url': f"/api/converted/{iap_tif_filename}",
            'class_stats':  class_stats,
            'class_names':  display_class_names,
            'training_sample_counts': display_training_counts,
            'training_sample_borrowed_from': display_borrowed_from,
            'blocked_from_borrowing': blocked_from_borrowing,
            'message':      'IAP classification complete',
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── ENTRY POINT ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("Starting Flask server...")
    app.run(debug=True, host='0.0.0.0', port=5000)
