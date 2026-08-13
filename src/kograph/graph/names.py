"""회사명 정규화 — 그래프 노드 동일성 판정용.

같은 회사가 공시마다 다른 표기로 등장한다:
    'SK하이닉스', 'SK하이닉스(SK Hynix Inc.)'
    '에코프로비엠', '(주)에코프로비엠', '주식회사 에코프로비엠'
정규화하지 않으면 별개 노드가 되어 2-hop 질의가 끊긴다.

과도한 병합은 서로 다른 회사를 합치므로, 법인격 표기와 병기된 영문명만
제거하고 상호 본체는 건드리지 않는다.
"""

import re

# 앞에 붙는 법인격 표기
_LEGAL_PREFIX = re.compile(r"^\s*(주식회사|㈜|\(주\)|\(유\)|유한회사|재단법인|사단법인)\s*")
# 뒤에 붙는 법인격 표기
_LEGAL_SUFFIX = re.compile(
    r"\s*(주식회사|㈜|\(주\)|\(유\)|유한회사|"
    r"Incorporated|Inc\.?|Corporation|Corp\.?|"
    r"Co\.?,?\s*Ltd\.?|Limited|Ltd\.?|LLC|L\.L\.C\.?|"
    r"GmbH|S\.A\.S|S\.A\.|B\.V\.|N\.V\.|Pte\.?\s*Ltd\.?|Zrt\.?)\s*[.,]?\s*$",
    re.IGNORECASE,
)
# 끝에 병기된 괄호 — 라틴 문자를 포함할 때만 (영문 사명 병기로 간주)
_TRAILING_PAREN = re.compile(r"\s*\(([^()]*[A-Za-z][^()]*)\)\s*$")
_WS = re.compile(r"\s+")


def canonical_name(raw: str) -> str:
    """노드 키로 쓸 정규형. 전부 깎여 빈 문자열이 되면 원문을 돌려준다."""
    name = _WS.sub(" ", (raw or "").strip())
    if not name:
        return ""

    previous = None
    while previous != name:
        previous = name
        name = _TRAILING_PAREN.sub("", name).strip()
        name = _LEGAL_SUFFIX.sub("", name).strip()
        name = _LEGAL_PREFIX.sub("", name).strip()

    return name or _WS.sub(" ", raw.strip())
