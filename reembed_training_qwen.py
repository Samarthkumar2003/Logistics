"""
reembed_training_qwen.py
------------------------
Re-embed all email_training_data rows using a local Qwen embedding model
(Alibaba-NLP/gte-Qwen2-1.5B-instruct) running on GPU.

Writes embeddings back to Supabase into the `embedding_qwen` column.
Run once; subsequent classifier runs can use these instead of OpenAI embeddings.

Usage:
    $env:CUDA_VISIBLE_DEVICES = "1"
    $env:QWEN_DEVICE = "cuda"
    $env:QWEN_BATCH_SIZE = "64"
    python reembed_training_qwen.py
"""

import json
import os
import time
import logging

import numpy as np
import torch
from dotenv import load_dotenv
from supabase import create_client
from transformers import AutoTokenizer, AutoModel

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MODEL_NAME = os.getenv("QWEN_MODEL", "Alibaba-NLP/gte-Qwen2-1.5B-instruct")
DEVICE = os.getenv("QWEN_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
BATCH_SIZE = int(os.getenv("QWEN_BATCH_SIZE", "32"))
MAX_LENGTH = 512

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"],
)


def mean_pool(token_embeddings: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    mask = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
    return torch.sum(token_embeddings * mask, dim=1) / torch.clamp(mask.sum(dim=1), min=1e-9)


def embed_batch(texts: list[str], tokenizer, model) -> np.ndarray:
    encoded = tokenizer(
        texts,
        padding=True,
        truncation=True,
        max_length=MAX_LENGTH,
        return_tensors="pt",
    ).to(DEVICE)
    with torch.no_grad():
        out = model(**encoded)
    vecs = mean_pool(out.last_hidden_state, encoded["attention_mask"])
    vecs = torch.nn.functional.normalize(vecs, p=2, dim=1)
    return vecs.cpu().float().numpy()


def main() -> None:
    logger.info("Device: %s | Model: %s | Batch: %d", DEVICE, MODEL_NAME, BATCH_SIZE)

    logger.info("Loading tokenizer and model…")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, trust_remote_code=True)
    model = AutoModel.from_pretrained(MODEL_NAME, trust_remote_code=True).to(DEVICE)
    model.eval()
    logger.info("Model loaded.")

    logger.info("Fetching training rows from Supabase…")
    rows = supabase.table("email_training_data").select("id, content, subject, label").execute().data
    logger.info("Fetched %d rows.", len(rows))

    texts = [
        f"Subject: {r.get('subject', '')}\n\n{(r.get('content') or '')[:1000]}"
        for r in rows
    ]

    all_vecs: list[np.ndarray] = []
    t0 = time.time()
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        vecs = embed_batch(batch, tokenizer, model)
        all_vecs.append(vecs)
        done = min(i + BATCH_SIZE, len(texts))
        elapsed = time.time() - t0
        rate = done / elapsed
        logger.info("  %d/%d embedded (%.1f rows/s)", done, len(texts), rate)

    embeddings = np.vstack(all_vecs)
    logger.info("All embeddings shape: %s", embeddings.shape)

    logger.info("Writing embeddings back to Supabase…")
    errors = 0
    for row, vec in zip(rows, embeddings):
        try:
            supabase.table("email_training_data").update(
                {"embedding_qwen": vec.tolist()}
            ).eq("id", row["id"]).execute()
        except Exception as e:
            logger.error("Failed to update row %s: %s", row["id"], e)
            errors += 1

    total_time = time.time() - t0
    logger.info(
        "Done. %d rows embedded in %.1fs. %d errors.",
        len(rows), total_time, errors,
    )
    logger.info(
        "Next: set QWEN_EMBEDDINGS=1 in .env and restart api.py to use these embeddings."
    )


if __name__ == "__main__":
    main()
