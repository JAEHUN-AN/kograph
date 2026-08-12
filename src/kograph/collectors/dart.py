"""DART OpenAPI collector.

- corp_codes(): 전체 기업 고유번호 매핑 (corpCode.xml zip)
- filings(): 기간·기업별 공시 목록 (list.json, 페이지네이션)

API docs: https://opendart.fss.or.kr/guide/main.do
"""

import io
import logging
import zipfile
from collections.abc import Iterator
from datetime import date
from xml.etree import ElementTree

import requests
from pydantic import BaseModel, Field, field_validator
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

BASE_URL = "https://opendart.fss.or.kr/api"
PAGE_SIZE = 100  # DART 최대값
REQUEST_TIMEOUT = 30

# DART 오류코드 중 "정상" 및 "조회 결과 없음"
_STATUS_OK = "000"
_STATUS_NO_DATA = "013"


class CorpInfo(BaseModel):
    corp_code: str = Field(min_length=8, max_length=8)
    corp_name: str
    stock_code: str | None = None
    modify_date: str | None = None

    @field_validator("stock_code", mode="before")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        return v or None


class Filing(BaseModel):
    rcept_no: str = Field(min_length=14, max_length=14)
    corp_code: str
    corp_name: str
    stock_code: str | None = None
    report_nm: str
    rcept_dt: str = Field(pattern=r"^\d{8}$")  # YYYYMMDD
    flr_nm: str | None = None
    rm: str | None = None

    @field_validator("stock_code", mode="before")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        return v or None


class DartApiError(RuntimeError):
    """DART가 정상(000) 이외의 상태를 반환했을 때."""

    def __init__(self, status: str, message: str):
        self.status = status
        super().__init__(f"DART API error {status}: {message}")


class DartClient:
    def __init__(self, api_key: str, session: requests.Session | None = None):
        if not api_key:
            raise ValueError("DART_API_KEY is required")
        self._api_key = api_key
        self._session = session or requests.Session()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10), reraise=True)
    def _get(self, endpoint: str, **params) -> requests.Response:
        resp = self._session.get(
            f"{BASE_URL}/{endpoint}",
            params={"crtfc_key": self._api_key, **params},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp

    def corp_codes(self) -> list[CorpInfo]:
        """전체 기업 고유번호 목록 (zip 안의 CORPCODE.xml)."""
        resp = self._get("corpCode.xml")
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_bytes = zf.read(zf.namelist()[0])
        root = ElementTree.fromstring(xml_bytes)
        corps = [
            CorpInfo(
                corp_code=el.findtext("corp_code", "").strip(),
                corp_name=el.findtext("corp_name", "").strip(),
                stock_code=el.findtext("stock_code"),
                modify_date=el.findtext("modify_date"),
            )
            for el in root.iter("list")
        ]
        logger.info("corp_codes: %d entries", len(corps))
        return corps

    def filings(
        self,
        begin: date,
        end: date,
        corp_code: str | None = None,
        pblntf_ty: str | None = None,  # A:정기 B:주요사항 ... I:거래소
    ) -> Iterator[Filing]:
        """공시 목록 페이지네이션 순회. 결과 없음(013)은 빈 이터레이터."""
        page = 1
        while True:
            params = {
                "bgn_de": begin.strftime("%Y%m%d"),
                "end_de": end.strftime("%Y%m%d"),
                "page_no": page,
                "page_count": PAGE_SIZE,
            }
            if corp_code:
                params["corp_code"] = corp_code
            if pblntf_ty:
                params["pblntf_ty"] = pblntf_ty

            body = self._get("list.json", **params).json()
            status = body.get("status", "")
            if status == _STATUS_NO_DATA:
                return
            if status != _STATUS_OK:
                raise DartApiError(status, body.get("message", "unknown"))

            for row in body.get("list", []):
                yield Filing.model_validate(row)

            if page >= int(body.get("total_page", 1)):
                return
            page += 1
