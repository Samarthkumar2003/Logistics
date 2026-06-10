# Email Classifier — Qwen3 Embeddings + MLP

The email classifier (`email_classifier.py`) runs a 4-tier pipeline:

1. **Rules** — structural signals (RFQ refs, known-agent sender, internal domains, job refs)
2. **Fine-tuned GPT-4o-mini** — optional, enabled via `CLASSIFIER_MODEL_ID`
3. **Qwen3-Embedding-0.6B + MLP** — local instruction-aware embeddings → sklearn MLP head
4. **GPT-4o-mini few-shot** — fallback when the MLP is below the confidence gate

Tier 3 embeds every email in **query mode**: the text is prefixed with an instruction
(`EMPIRICAL_BASIS`) describing the asking-vs-providing classification rule. Training vectors
and runtime queries use the **same** mode, so the MLP trains and infers on matching features.

Vectors are 1024-dim, stored in the `email_training_data.embedding_qwen` column.

---

## Hardware / device selection

The Qwen model loads onto the device set by `QWEN_DEVICE`:

| device | when to use | notes |
|--------|-------------|-------|
| `cpu` (default) | no GPU, or Apple Silicon | slow (~45 min to embed ~1000 rows) |
| `cuda` | NVIDIA GPU (e.g. RTX 3050) | fp16 auto-on; fast (minutes) |
| `mps` | Apple GPU | currently mis-sizes buffers — leave on `cpu` |

**AMD GPUs are not supported** (CUDA is NVIDIA-only). On a machine with both an NVIDIA and an
AMD card, pin to the NVIDIA one with `CUDA_VISIBLE_DEVICES`.

### Config (environment variables)

| var | default | meaning |
|-----|---------|---------|
| `QWEN_DEVICE` | `cpu` | `cpu` \| `cuda` \| `mps` |
| `QWEN_BATCH_SIZE` | `16` | encode batch; auto-OOM fallback halves this on GPU OOM |
| `QWEN_FP16` | auto (`1` on cuda) | force fp16 on/off; halves memory + ~2x throughput |
| `QWEN_EMBED_MODEL` | `Qwen/Qwen3-Embedding-0.6B` | swap to 4B/8B for higher accuracy |

The encode path has an **auto-OOM fallback**: on a CUDA/MPS out-of-memory error it frees the
cache, halves the batch, and retries down to batch=1. So a 4GB GPU degrades gracefully rather
than crashing — but keep the batch small enough to stay inside dedicated VRAM (spilling into
"shared" system memory works but is 10–50x slower).

---

## Running on an NVIDIA GPU (e.g. RTX 3050, 4GB)

### 1. Install a CUDA build of torch

The default `pip install torch` from `requirements.txt` is a CPU build. On a CUDA box, install
the CUDA wheel matching your driver (`cu121`, `cu124`, …):

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install -r requirements.txt
```

Verify CUDA is visible:

```bash
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

### 2. The three exports

```bash
export CUDA_VISIBLE_DEVICES=0     # pin to the NVIDIA card (hides any AMD GPU)
export QWEN_DEVICE=cuda           # load Qwen on the GPU; fp16 turns on automatically
export QWEN_BATCH_SIZE=16         # conservative for 4GB VRAM; raise to 32/64 on bigger GPUs
```

> If `nvidia-smi` lists your NVIDIA card as something other than index 0, set
> `CUDA_VISIBLE_DEVICES` to that index instead.

### 3. Populate the embedding column

One-time DDL in the Supabase SQL editor (requires the `pgvector` extension):

```sql
ALTER TABLE email_training_data ADD COLUMN IF NOT EXISTS embedding_qwen vector(1024);
```

Then embed all rows in query mode and **upload them to Supabase**:

```bash
python reembed_training_qwen.py            # only rows missing embedding_qwen
python reembed_training_qwen.py --all      # re-embed everything (overwrite)
```

This writes each vector back to `email_training_data.embedding_qwen` (paginates past Supabase's
1000-row cap, so all rows are covered).

### 4. Benchmark (85/15 split)

```bash
python eval_classifier.py                  # reads embedding_qwen from Supabase
python eval_classifier.py --embed-live     # embeds in-memory (no DB column needed)
python eval_svm_baseline.py                # legacy SVM + OpenAI embeddings, for comparison
```

### 5. Run the API

```bash
uvicorn api:app --port 8001
```

The classifier warms up Qwen + the MLP at startup. Check the active config at
`GET /classifier-status`.

---

## Quick reference — full GPU run

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install -r requirements.txt

export CUDA_VISIBLE_DEVICES=0
export QWEN_DEVICE=cuda
export QWEN_BATCH_SIZE=16

# (run the ALTER TABLE in Supabase once)
python reembed_training_qwen.py --all
python eval_classifier.py
uvicorn api:app --port 8001
```
