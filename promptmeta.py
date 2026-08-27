"""Boil a ComfyUI prompt graph down to a per-tile metadata record.

The hidden `prompt` input is the whole API graph -- tens of KB, and mostly
uninteresting once you have the image.  What a map sidebar wants is the handful
of fields a human recognises: what was asked for, with which model, at which
seed.  Everything here is best-effort by design: the extractor must not raise on
an unfamiliar graph, it should just return fewer keys.
"""
import datetime

# Anything whose class name contains one of these is treated as "the sampler",
# which is where the search for seed/steps/cfg starts.  Substring matching on
# purpose: KSampler, KSamplerAdvanced, KSampler (Efficient), SamplerCustom,
# SamplerCustomAdvanced and CFGGuider all need to match, and packs keep adding
# their own spellings.
_SAMPLERISH = ("ksampler", "samplercustom", "cfgguider", "sampler")
_SCALARS = ("seed", "noise_seed", "steps", "cfg", "guidance", "sampler_name",
            "scheduler", "denoise", "shift")
_MODEL_SUFFIXES = (".safetensors", ".ckpt", ".gguf", ".pt", ".pth", ".sft")
_TEXT_KEYS = ("text", "text_g", "text_l", "prompt", "populated_text",
              "wildcard_text")


def _is_link(value):
    return isinstance(value, list) and len(value) == 2 and isinstance(value[1], int)


def _nodes_like(prompt, needles):
    out = []
    for nid, node in prompt.items():
        cls = str(node.get("class_type", "")).lower()
        if any(n in cls for n in needles):
            out.append((nid, node))
    return out


def _chain_classes(prompt, ref, depth=4, seen=None):
    """Lower-cased class names on the way upstream from a link."""
    if not _is_link(ref) or depth <= 0:
        return set()
    seen = seen if seen is not None else set()
    nid = str(ref[0])
    if nid in seen:
        return set()
    seen.add(nid)
    node = prompt.get(nid) or prompt.get(ref[0])
    if not isinstance(node, dict):
        return set()
    out = {str(node.get("class_type", "")).lower()}
    for value in node.get("inputs", {}).values():
        if _is_link(value):
            out |= _chain_classes(prompt, value, depth - 1, seen)
    return out


def _trace_text(prompt, ref, depth=8, seen=None):
    """Walk back from a conditioning link to the text that produced it."""
    if not _is_link(ref) or depth <= 0:
        return None
    seen = seen if seen is not None else set()
    nid = str(ref[0])
    if nid in seen:
        return None
    seen.add(nid)
    node = prompt.get(nid) or prompt.get(ref[0])
    if not isinstance(node, dict):
        return None
    inputs = node.get("inputs", {})
    for key in _TEXT_KEYS:
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in inputs.values():           # keep walking upstream
        if _is_link(value):
            found = _trace_text(prompt, value, depth - 1, seen)
            if found:
                return found
    return None


def _has_linked_text(prompt, samplers):
    """True when a sampler's positive encoder takes its text from a wire.

    That is the signature of a prompt assembled at runtime, which no amount of
    graph walking can recover — worth saying so rather than leaving the field
    silently empty.
    """
    for _, node in samplers:
        ref = node.get("inputs", {}).get("positive")
        if not _is_link(ref):
            continue
        src = prompt.get(str(ref[0])) or prompt.get(ref[0])
        if isinstance(src, dict):
            for key in _TEXT_KEYS:
                if _is_link(src.get("inputs", {}).get(key)):
                    return True
    return False


def summarize_prompt(prompt, extra_pnginfo=None, extra=None):
    """-> a small dict; never raises on a graph it does not recognise."""
    meta = {"time": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    if not isinstance(prompt, dict):
        prompt = {}

    samplers = _nodes_like(prompt, _SAMPLERISH)
    sampler_ids = {nid for nid, _ in samplers}
    ordered = [n for _, n in samplers] + [
        n for nid, n in prompt.items() if nid not in sampler_ids
    ]

    for key in _SCALARS:
        for node in ordered:
            value = node.get("inputs", {}).get(key)
            if value is not None and not _is_link(value):
                meta["seed" if key == "noise_seed" else key] = value
                break

    # Positive/negative by tracing the sampler's own conditioning inputs, which
    # is more reliable than "the first CLIPTextEncode in the graph" -- graphs
    # routinely hold several, including disconnected leftovers.
    for slot, name in (("positive", "prompt"), ("negative", "negative")):
        for _, node in samplers:
            ref = node.get("inputs", {}).get(slot)
            # Z-Image and Klein wire the negative through ConditioningZeroOut fed
            # by the *positive* text encoder. Tracing text through that reports
            # the positive prompt as if it were a negative one -- true to the
            # graph, wrong about the model.
            if slot == "negative" and "conditioningzeroout" in _chain_classes(prompt, ref):
                meta[name] = "(zeroed)"
                break
            text = _trace_text(prompt, ref)
            if text and text != meta.get("prompt"):
                meta[name] = text
                break
    if "prompt" not in meta:
        # Last resort: any text encoder with a literal string. It must exclude
        # whatever was already identified as the negative, or a graph whose
        # positive text is *computed upstream* (FormattedString, a wildcard node,
        # a list selector -- the text is simply not in the graph) reports its
        # negative prompt as the positive one. That is not a cosmetic slip: it
        # reads as a plausible prompt and is entirely wrong.
        for _, node in _nodes_like(prompt, ("cliptextencode", "textencode")):
            for key in _TEXT_KEYS:
                value = node.get("inputs", {}).get(key)
                if isinstance(value, str) and value.strip() \
                        and value.strip() != meta.get("negative"):
                    meta["prompt"] = value.strip()
                    break
            if "prompt" in meta:
                break

    if "prompt" not in meta and _has_linked_text(prompt, samplers):
        meta["prompt_note"] = ("positive prompt is built upstream at runtime; "
                               "wire it into the saver's prompt_text to record it")

    models = []
    for node in prompt.values():
        for value in node.get("inputs", {}).values():
            if isinstance(value, str) and value.lower().endswith(_MODEL_SUFFIXES):
                if value not in models:
                    models.append(value)
    if models:
        meta["models"] = models

    for node in prompt.values():
        inputs = node.get("inputs", {})
        w, h = inputs.get("width"), inputs.get("height")
        if isinstance(w, (int, float)) and isinstance(h, (int, float)):
            meta["latent_size"] = [int(w), int(h)]
            break

    if isinstance(extra_pnginfo, dict):
        workflow = extra_pnginfo.get("workflow")
        if isinstance(workflow, dict):
            for key in ("id", "revision"):
                if key in workflow:
                    meta[f"workflow_{key}"] = workflow[key]

    if extra:
        meta.update({k: v for k, v in extra.items() if v not in (None, "", [])})
    return meta
