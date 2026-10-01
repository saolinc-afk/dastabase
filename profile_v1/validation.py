"""Deterministic citation, enum, scope and publication gates."""
from profile_v1.contracts import CLAIM_TYPES, ENUMS
from profile_v1.store import encode, uid


def validate_and_store(store, context, candidates, interpreter_version):
    blocks={r['block_id']:dict(r) for r in store.conn.execute('''
        SELECT b.*,e.source_class FROM profile_content_blocks b JOIN profile_evidence e
        ON e.evidence_id=b.evidence_id WHERE b.attempt_id=? AND b.company_id=?''',
        (context['attempt_id'],context['company_id']))}
    ordered=sorted(enumerate(candidates), key=lambda item:item[1].claim_type=='BUSINESS_DESCRIPTION')
    outcomes={}; supported_blocks=set()
    for index,candidate in ordered:
        reason=None
        if candidate.claim_type not in CLAIM_TYPES: reason='UNSUPPORTED_CLAIM_TYPE'
        elif candidate.confidence not in ('HIGH','MEDIUM','LOW'): reason='INVALID_CONFIDENCE'
        elif not candidate.display_value.strip() or candidate.normalized_value is None: reason='VALUE_REQUIRED'
        elif candidate.claim_type in ENUMS and candidate.normalized_value not in ENUMS[candidate.claim_type]: reason='INVALID_ENUM'
        elif not candidate.citations: reason='EVIDENCE_REQUIRED'
        else:
            for citation in candidate.citations:
                block=blocks.get(citation.block_id)
                if block is None: reason='MISSING_BLOCK'; break
                if block['source_class']!='FIRST_PARTY': reason='SOURCE_CLASS_NOT_PERMITTED'; break
                if not citation.quote or citation.quote not in block['text']: reason='FABRICATED_QUOTE'; break
        if not reason and candidate.claim_type=='BUSINESS_DESCRIPTION':
            cited={c.block_id for c in candidate.citations}
            if not cited & supported_blocks: reason='DESCRIPTION_REQUIRES_SUPPORTED_CLAIM'
        outcomes[index]=(candidate,reason)
        if not reason and candidate.claim_type!='BUSINESS_DESCRIPTION':
            supported_blocks.update(c.block_id for c in candidate.citations)
    supported=[]; rejected=[]
    for index in range(len(candidates)):
        candidate,reason=outcomes[index]
        claim_id=uid(); status='REJECTED' if reason else 'SUPPORTED'
        store.insert('profile_claims',{**context,'claim_id':claim_id,'claim_type':candidate.claim_type,
            'normalized_value_json':encode(candidate.normalized_value),'display_value':candidate.display_value,
            'confidence':candidate.confidence,'status':status,'ambiguity_note':candidate.ambiguity_note,
            'rejection_reason':reason,'interpreter_version':interpreter_version})
        if not reason:
            for citation in candidate.citations:
                store.insert('profile_claim_evidence',{'claim_id':claim_id,'block_id':citation.block_id,'quote':citation.quote})
            supported.append(claim_id)
        else: rejected.append(claim_id)
    return supported,rejected
