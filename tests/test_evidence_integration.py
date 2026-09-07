from __future__ import annotations
from pathlib import Path
from sightmesh.evidence_index import EvidenceIndex
from sightmesh.evidence import EvidenceClient, EvidenceUnavailable
from sightmesh.retention import CheckpointRetention
from sightmesh.usage import derive

class Native:
    def __init__(self): self.calls=[]; self.blobs={}
    def artifact(self, execution_id, **kw):
        key=kw['publication_key']; self.calls.append(key); self.blobs.setdefault(key, kw['body'])
        import hashlib
        return {'id': key, 'execution_id':execution_id, 'publication_key':key, 'producer_ref':kw['producer_ref'], 'original_path':kw['original_path'], 'durability':'confirmed','sha256':hashlib.sha256(kw['body']).hexdigest(),'size_bytes':len(kw['body'])}
    def artifact_bytes(self, execution_id, occurrence_id): return self.blobs[occurrence_id]

class Response:
    def __init__(self, body, headers=()): self.body=body; self.headers=dict(headers); self.at=0
    def read(self, size=-1):
        end=len(self.body) if size < 0 else min(len(self.body), self.at+size)
        out=self.body[self.at:end]; self.at=end; return out
    def __enter__(self): return self
    def __exit__(self, *_): return False

def test_raw_range_reads_the_native_bracket_header_and_rejects_wrong_identity():
    replies=[Response(b'{"data":{"service_capabilities":{"execution_evidence":1}}}'), Response(b'abc', [('x-cdesktop-source-id','exec'),('x-cdesktop-source-range','[4, 7)'),('x-cdesktop-source-durability','confirmed')])]
    client=EvidenceClient('http://test', opener=lambda _request: replies.pop(0))
    assert client.raw('exec',4,8).body == b'abc'
    bad=[Response(b'{"data":{"service_capabilities":{"execution_evidence":1}}}'), Response(b'x', [('x-cdesktop-source-id','other'),('x-cdesktop-source-range','[0, 1)'),('x-cdesktop-source-durability','confirmed')])]
    with __import__('pytest').raises(EvidenceUnavailable): EvidenceClient('http://test', opener=lambda _request: bad.pop(0)).raw('exec',0,1)

def test_equal_checkpoint_bytes_are_distinct_operations_but_a_retry_reuses_one(tmp_path):
    source=tmp_path/'checkpoint.md'; source.write_text('same')
    native=Native(); keep=CheckpointRetention(tmp_path/'task.sqlite', native, 'exec')
    first=keep.retain('task', 1, source)
    second=keep.retain('task', 1, source)
    assert first != second
    assert keep.retain('task', 1, source, first.removeprefix('checkpoint:')) == first

def test_index_keeps_only_locator_and_uses_compressed_start_for_empty_frames(tmp_path):
    index=EvidenceIndex(tmp_path/'index.sqlite')
    index.ingest('e','source',31,0,'job.id: one',cursor='0',confirmed=True)
    index.ingest('e','source',32,0,'',cursor='1',confirmed=True)
    rows=index.search('job.id')
    assert [(r[0],r[3]) for r in rows] == [('source',31)]

def test_usage_dedupes_request_events_and_preserves_unknown():
    assert derive([{'request_id':'a','input_tokens':2,'output_tokens':3},{'request_id':'a','input_tokens':2,'output_tokens':3}]) == {'tokens':5}
    assert derive([{'request_id':'a'}]) == {'tokens':None}
