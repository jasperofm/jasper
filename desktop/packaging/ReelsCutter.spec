# PyInstaller spec: builds "Reels Cutter.app" (one folder, windowed). Run from the desktop/ folder:
#   pyinstaller packaging/ReelsCutter.spec --noconfirm
import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.getcwd())
datas = [(os.path.join(ROOT, "assets"), "assets")]
binaries = []
hidden = collect_submodules("reelscutter")
datas += collect_data_files("imageio_ffmpeg", include_py_files=False)
binaries += collect_dynamic_libs("imageio_ffmpeg")
datas += collect_data_files("cv2", includes=["data/*.xml"])
for opt in ("faster_whisper", "ctranslate2", "tokenizers", "onnxruntime"):
    try:
        __import__(opt)
        datas += collect_data_files(opt)
        binaries += collect_dynamic_libs(opt)
        hidden += collect_submodules(opt)
    except Exception:
        pass

EXCLUDES = ["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
            "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DExtras", "PySide6.QtQuick3D", "PySide6.QtCharts",
            "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
            "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSensors", "PySide6.QtSerialPort",
            "PySide6.QtLocation", "PySide6.QtPositioning", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
            "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtTextToSpeech", "PySide6.QtWebSockets",
            "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtSql", "PySide6.QtTest", "matplotlib", "IPython", "pandas",
            "torch", "onnx"]

a = Analysis([os.path.join(ROOT, "run_app.py")], pathex=[ROOT], binaries=binaries, datas=datas,
             hiddenimports=hidden, excludes=EXCLUDES, noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Reels Cutter", console=False,
          target_arch=os.environ.get("TARGET_ARCH") or None, codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Reels Cutter")
app = BUNDLE(coll, name="Reels Cutter.app",
             icon=os.path.join(ROOT, "packaging", "ReelsCutter.icns") if os.path.exists(
                 os.path.join(ROOT, "packaging", "ReelsCutter.icns")) else None,
             bundle_identifier="com.jasper.reelscutter",
             info_plist={"CFBundleShortVersionString": "1.0.0", "CFBundleVersion": "1.0.0",
                         "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "12.0",
                         "NSRequiresAquaSystemAppearance": False,
                         "CFBundleDocumentTypes": [{"CFBundleTypeName": "Video", "CFBundleTypeRole": "Viewer",
                                                    "LSItemContentTypes": ["public.movie"]}]})
