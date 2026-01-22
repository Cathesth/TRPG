
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from typing import List

from models import get_db, Scenario
from routes.auth import get_current_user, CurrentUser

router = APIRouter(prefix="/api/admin", tags=["admin"])

@router.get("/scenarios", summary="관리자용 시나리오 목록 (추천 설정용)")
async def get_admin_scenarios(
    current_user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # 권한 체크 (ID: 11)
    if current_user.id != '11':
        raise HTTPException(status_code=403, detail="관리자 권한이 없습니다.")

    # 공개된 모든 시나리오 가져오기 (관리 목적이므로 내림차순 정렬)
    scenarios = db.query(Scenario).filter(Scenario.is_public == True).order_by(Scenario.id.desc()).all()
    
    return [
        {
            "id": s.id,
            "title": s.title,
            "author": s.author_id,
            "is_recommended": s.is_recommended or False,
            "updated_at": s.updated_at
        }
        for s in scenarios
    ]

@router.post("/recommend", summary="추천 시나리오 설정")
async def set_recommended_scenarios(
    scenario_ids: List[int] = Body(..., embed=True),
    current_user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.id != '11':
        raise HTTPException(status_code=403, detail="관리자 권한이 없습니다.")

    # 1. 모든 시나리오의 is_recommended를 False로 초기화
    # 주의: update 쿼리는 트랜잭션 내에서 실행됨
    db.query(Scenario).update({Scenario.is_recommended: False})
    
    # 2. 선택된 시나리오들을 True로 설정
    if scenario_ids:
        db.query(Scenario).filter(Scenario.id.in_(scenario_ids)).update(
            {Scenario.is_recommended: True}, 
            synchronize_session=False
        )
    
    db.commit()
    return {"success": True, "count": len(scenario_ids)}
