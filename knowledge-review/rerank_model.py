"""Local multilingual cross-encoder; explicit download, pinned safe weights.

Encode the query/document pair jointly with truncation disabled. Reranking scores
are logits used for ordering, not probabilities or document knowledge validation.
"""
from dataclasses import dataclass, asdict
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
import json
import os
import re

ROOT = Path(__file__).resolve().parent
MODEL_CACHE = ROOT / '.models'
MODEL_NAME = 'cross-encoder/mmarco-mMiniLMv2-L12-H384-v1'
MODEL_REVISION = '1427fd652930e4ba29e8149678df786c240d8825'


@dataclass(frozen=True)
class RerankConfig:
    model_name: str = MODEL_NAME
    revision: str = MODEL_REVISION
    max_tokens: int = 512
    batch_size: int = 8
    max_windows: int = 16
    max_pairs: int = 1024

    def __post_init__(self):
        if not re.fullmatch(r'[\w.-]+/[\w.-]+',self.model_name) or len(self.revision) != 40 or any(c not in '0123456789abcdef' for c in self.revision):
            raise ValueError('重排模型必须提供名称及固定commit身份。')
        for value in (self.max_tokens, self.batch_size, self.max_windows, self.max_pairs):
            if type(value) is not int or value <= 0:
                raise ValueError('重排预算及批大小必须是正整数。')


def model_home(config,cache):
    return Path(cache)/('rerank--'+config.model_name.replace('/','--'))/config.revision


def download_reranker(config=RerankConfig(), cache=MODEL_CACHE):
    """Only this explicit command may download; ordinary rerank is offline-only."""
    from huggingface_hub import snapshot_download
    os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
    # local_dir uses ordinary inherited-permission directories; avoids the Hub's
    # Windows cache symlink probe creating an inaccessible mode-0700 temp folder.
    target=model_home(config,cache)
    target.mkdir(parents=True,exist_ok=True)
    return snapshot_download(config.model_name, revision=config.revision, local_dir=str(target), token=False,
                             allow_patterns=['*.json', '*.safetensors', '*.model'], max_workers=2)


class LocalReranker:
    def __init__(self, config=RerankConfig(), cache=MODEL_CACHE):
        target=model_home(config,cache)
        if not target.is_dir():
            raise RuntimeError('本地重排模型不可用；先运行 quality_retrieval.py download-reranker。普通评测不自动下载。')
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        self.config = config
        os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
        options = {'local_files_only':True,
                   'trust_remote_code':False, 'token':False}
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(str(target), use_fast=True, **options)
            self.model = AutoModelForSequenceClassification.from_pretrained(str(target), use_safetensors=True, **options)
        except OSError as exc:
            raise RuntimeError('本地重排模型不可用；先运行 quality_retrieval.py download-reranker。普通评测不自动下载。') from exc
        if self.model.config.num_labels != 1:
            raise ValueError('当前重排接口要求一个排序logit，不能套用多分类概率。')
        self.model.to(device='cpu', dtype=torch.float32).eval()
        # Require a real tokenizer capacity rather than guessing a context size.
        capacity = self.tokenizer.model_max_length
        if type(capacity) is not int or not 4 <= capacity <= 65536:
            raise ValueError('重排tokenizer缺少可靠容量，不能猜测后静默截断。')
        positions = getattr(self.model.config,'max_position_embeddings',None)
        if type(positions) is not int or positions < 4:
            raise ValueError('重排模型缺少可靠位置容量。')
        # RoBERTa reserves padding positions and starts real position IDs at 2.
        position_capacity = positions-2 if self.model.config.model_type in ('roberta','xlm-roberta') else positions
        self.max_tokens = min(config.max_tokens, capacity, position_capacity)
        self.capacities={'tokenizer':capacity,'position':position_capacity}
        self._profile = None

    def token_count(self, query, document):
        return len(self.tokenizer(query, document, truncation=False, padding=False,
                                  add_special_tokens=True, verbose=False)['input_ids'])

    def predict(self, pairs):
        import torch
        result = []
        for offset in range(0, len(pairs), self.config.batch_size):
            batch = pairs[offset:offset+self.config.batch_size]
            if any(self.token_count(q, d) > self.max_tokens for q, d in batch):
                raise ValueError('重排输入超过实际容量；禁止自动截断。')
            features = self.tokenizer([q for q, _ in batch], [d for _, d in batch],
                                      truncation=False, padding=True, return_tensors='pt')
            if features['input_ids'].shape[1] > self.max_tokens:
                raise ValueError('重排实际张量超过预算。')
            with torch.inference_mode():
                values = self.model(**features).logits[:, 0].cpu().tolist()
            result.extend(values)
        return result

    def profile(self):
        if self._profile is None:
            import torch
            backend = getattr(self.tokenizer, 'backend_tokenizer', None)
            if backend is None:
                raise ValueError('模型指纹要求可序列化的实际tokenizer。')
            data = json.loads(backend.to_str())
            data.pop('padding', None)
            data.pop('truncation', None)
            fingerprint = sha256(json.dumps(data, sort_keys=True).encode())
            fingerprint.update(self.model.config.to_json_string().encode())
            for name, tensor in self.model.state_dict().items():
                fingerprint.update(f'{name}:{tensor.dtype}:{tuple(tensor.shape)}'.encode())
                fingerprint.update(tensor.detach().cpu().contiguous().reshape(-1).view(torch.uint8).numpy().tobytes())
            self._profile = {**asdict(self.config), 'effective_max_tokens':self.max_tokens,
                             'capacities':self.capacities,
                             'weights_tokenizer_fingerprint':fingerprint.hexdigest(),
                             'runtime':{n:version(n) for n in ('torch','transformers','tokenizers')},
                             'device':'cpu', 'dtype':'float32', 'activation':'identity/raw logit',
                             'input':'full query + original document character windows; no prefix/filename boost',
                             'window_aggregation':'maximum logit, stable B order for ties',
                             'truncation':False}
        return self._profile
