"""Experimental original-Wav2Lip INT8 calibration; never activates a runtime config."""
import argparse
import hashlib
import json
from pathlib import Path
import time
import sys
from cpu_avatar import CpuAvatar


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,required=True)
    parser.add_argument("--audio",type=Path,nargs="+",required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output directory; never overwrite evidence")
    args.output.mkdir(parents=True)
    report={"status":"RUNNING","quality_accepted":False,"runtime_activated":False,
            "scope":"experimental fixed-avatar calibration; not general face accuracy"}
    def save(): (args.output/"summary.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    save()
    try:
        import nncf
        import numpy as np
        import openvino as ov
        config=json.loads(args.config.read_text(encoding="utf-8"))
        config["avatar_batch"]=1
        avatar=CpuAvatar(config)
        core=ov.Core()
        graph=core.read_model(config["avatar_ir"])
        graph.reshape({graph.inputs[0]:[1,1,80,16],graph.inputs[1]:[1,6,96,96]})
        audio_name,face_name=[port.any_name for port in graph.inputs]
        calibration=[]
        report["inputs"]=[]
        for path in args.audio:
            _,_,mel=avatar.audio_features(str(path))
            for index in range(0,mel.shape[1]-15,4):
                calibration.append({audio_name:mel[:,index:index+16][None,None].astype("float32"),
                                    face_name:avatar.face_input})
            report["inputs"].append({"file":str(path),"sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
        if len(calibration)<16:
            raise ValueError("Calibration needs at least 16 actual mel windows")
        report.update(stage="quantization",samples=len(calibration),nncf=nncf.__version__)
        save()
        started=time.perf_counter()
        compressed=nncf.quantize(graph,nncf.Dataset(calibration),
            subset_size=len(calibration),preset=nncf.QuantizationPreset.MIXED,
            target_device=nncf.TargetDevice.CPU,fast_bias_correction=True)
        destination=args.output/"wav2lip-int8.xml"
        ov.save_model(compressed,destination,compress_to_fp16=False)
        # Held-out quality acceptance is separate; this measures conversion fidelity only.
        baseline=core.compile_model(graph,"CPU",{"INFERENCE_NUM_THREADS":8,"NUM_STREAMS":1,"INFERENCE_PRECISION_HINT":"f32"})
        quantized=core.compile_model(compressed,"CPU",{"INFERENCE_NUM_THREADS":8,"NUM_STREAMS":1})
        errors=[]
        for item in calibration[::max(1,len(calibration)//8)]:
            expected=baseline(item)[baseline.output(0)]
            actual=quantized(item)[quantized.output(0)]
            errors.append(float(np.mean(np.abs(expected-actual))))
        report.update(status="PASS",stage="complete",seconds=time.perf_counter()-started,
            model=str(destination),calibration_mae=errors,
            warning="Calibration error is not a held-out quality result; inspect video before use")
        return 0
    except Exception as exc:
        report.update(status="FAIL",error_type=type(exc).__name__,error=str(exc))
        return 1
    finally: save()

if __name__=="__main__": raise SystemExit(main())
