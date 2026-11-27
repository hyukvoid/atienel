import json
from pathlib import Path

import numpy as np
import onnxruntime as ort
from sentencepiece import SentencePieceProcessor

RUNBOOK_DIR = Path(__file__).resolve().parents[1] / "runbooks"
MODEL_DIR = Path("/model")


def manifest():
    return json.loads((RUNBOOK_DIR / "seed-manifest.json").read_text())


class Embedder:
    def __init__(self):
        config = manifest()["model"]
        self.prefixes = config["prefixes"]
        self.dimensions = config["dimensions"]
        self.tokenizer = SentencePieceProcessor(
            model_file=str(MODEL_DIR / "sentencepiece.bpe.model")
        )
        self.max_tokens = config["max_tokens"]
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.enable_cpu_mem_arena = False
        options.enable_mem_pattern = False
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
        options.add_session_config_entry("session.disable_prepacking", "1")
        ort.disable_telemetry_events()
        self.session = ort.InferenceSession(
            str(MODEL_DIR / "model.onnx"),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )

    def vector(self, text: str, mode: str) -> list[float]:
        pieces = self.tokenizer.encode(self.prefixes[mode] + text)
        # XLM-R uses <s>=0, </s>=2, <unk>=3 and offsets ordinary SentencePiece IDs by one.
        ids = [0, *(3 if piece == 0 else piece + 1 for piece in pieces[: self.max_tokens - 2]), 2]
        inputs = {
            "input_ids": ids,
            "attention_mask": [1] * len(ids),
            "token_type_ids": [0] * len(ids),
        }
        tensors = {
            item.name: np.array([inputs[item.name]], dtype=np.int64)
            for item in self.session.get_inputs()
        }
        hidden = self.session.run(None, tensors)[0][0]
        mask = tensors["attention_mask"][0, :, None]
        pooled = (hidden * mask).sum(axis=0) / mask.sum()
        norm = np.linalg.norm(pooled)
        if pooled.shape != (self.dimensions,) or not np.isfinite(norm) or norm <= 0:
            raise ValueError("Invalid embedding")
        return (pooled / norm).tolist()


def vector_parameter(vector: list[float]) -> str:
    return json.dumps(vector, separators=(",", ":"), allow_nan=False)
