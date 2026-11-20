# Compliance fine-tuning

This workflow prepares a reviewed dataset, trains a QLoRA adapter, compares it with
the base model, and loads an explicitly selected adapter in the existing Colab
gateway. No trained weights are included. No training has run simply by installing
these files. Codex mode continues to use its configured CLI model; these adapters
are for the open-weight HTTP/Colab inference model.

Fine-tuning may improve grading consistency, JSON validity and concise output.
It does not inherently increase a 9B model's token generation speed. Measure
total time and accuracy with the same held-out cases before selecting it.

## Data

`review-cases.example.jsonl` has 10 fictional examples covering C, OFI, NC-Minor,
NC-Major and Needs Review. They are intentionally **unapproved templates**. Replace
them with auditor-reviewed cases and fill `approved: true` and `reviewed_by` only
after checking the complete expected finding. Do not copy an unreviewed AI result
as a label. Include Thai answers, shorthand, multiple interview answers, actual
gaps, valid passes without attachments, and ambiguous/contradictory responses.
Use only records you are authorized to train on; remove customer identifiers and
credentials before moving them to Colab. Knowledge PDFs alone do not supply graded
examples, and are not ingested by this workflow.

Each JSONL row contains `case_id`, `group_id`, `approved`, `reviewed_by`,
`control_id`, `requirement`, `answer`, `context`, and the full expected `finding`.
Use one `group_id` for all variants from the same company/process/case family so
they cannot be split between training and evaluation. The preparation command
rejects duplicate requirement/answer pairs and validates finding consistency.
For real training, collect hundreds of varied reviewed cases rather than relying
on these 10 examples; the useful sample count depends on coverage and label quality.

```bash
python inference/finetune/compliance_tune.py prepare --input reviewed-cases.jsonl --output training-data
```

For an explicitly reviewed fictional demonstration only, add `--allow-demo`.
Preparation writes group-separated `train.jsonl`, `eval.jsonl`, and a manifest.
Inspect both class distributions before training; group splitting does not promise
that every category appears in both sets. Keep a separate final audit set untouched
by training, tuning and prompt changes.

## Colab

Open `colab_finetune.ipynb`. Upload/extract the repository under
`/content/sc-ai/platform`, upload reviewed cases, install requirements, prepare data,
then train. The notebook does not mount Drive or publish weights automatically.

```bash
python inference/finetune/compliance_tune.py train --data training-data --output compliance-adapter --model Qwen/Qwen3.5-9B --max-length 2048
```

Defaults: 4-bit NF4, text-only LoRA rank 8, batch 1, gradient accumulation 8,
gradient checkpointing, two epochs. Only assistant completion tokens contribute
to the loss. The base model remains frozen. Overlength examples fail instead of
silently dropping requirement/answer/output tokens. Qwen3.5 uses its conditional
generation class; vision projections are excluded from adapter targets.

A 15 GB GPU may still run out of memory with 9B training, depending on dependencies
and sequence lengths. Stop the gateway first. If memory is insufficient, use a
larger GPU, or select a smaller supported base model and train/load its matching
adapter. Reduce max length only when complete examples fit. Do not silently move
layers to CPU or truncate records to make training appear successful.

## Compare and enable

Run baseline and adapter evaluations in separate fresh GPU processes/sessions
with the same model, test file and max-new-tokens. Reinstall dependencies and
restore dataset/adapter after a Colab runtime reset.

```bash
python inference/finetune/compliance_tune.py evaluate --input training-data/eval.jsonl --output base-report.json
python inference/finetune/compliance_tune.py evaluate --input training-data/eval.jsonl --adapter compliance-adapter --output adapter-report.json
```

Reports include category agreement, macro F1 over present classes, NC-Major recall,
JSON/schema validity, completion tokens and generation seconds after warmup.
Generation time excludes model loading, network, application validation and queuing.
These are single-control evaluation prompts; also run the actual app with its
selected skills and normal batches before enabling the adapter for real assessments.
Do not deploy if C is overpredicted, NC-Major recall worsens, or JSON failures grow.

Back up the adapter directory before Colab expires. Re-upload the updated
`inference/server.py` and `inference/requirements.txt`, install dependencies, then
set this in the **inference notebook** before starting the gateway:

```python
os.environ['AI_MODEL'] = 'Qwen/Qwen3.5-9B'
os.environ['AI_LORA_ADAPTER'] = '/content/compliance-adapter'
```

The gateway checks that the adapter's base model exactly matches `AI_MODEL`.
Restart and warm up the gateway; authenticated `/health` should show
`adapter_configured: true` and `adapter_loaded: true`. The backend HTTP provider
and its existing validation remain in use. The full adapter directory, not only
its weight file, is needed. Clear `AI_LORA_ADAPTER` and restart to roll back.
The adapter is enabled only for `assess`; the gateway temporarily disables it for
question generation and department/relevance mapping because these tasks were
not part of this grading training dataset.

Implementation references: [Hugging Face PEFT quantization](https://huggingface.co/docs/peft/developer_guides/quantization),
[LoRA](https://huggingface.co/docs/peft/package_reference/lora), and
[Transformers Trainer](https://huggingface.co/docs/transformers/main_classes/trainer).
