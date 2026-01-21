import os
# CustomNPC도 임포트하여 Base.metadata에 확실히 등록되도록 함
from models import engine, Preset, Base, CustomNPC

def reset_presets_table():
    print("🔄 Presets 테이블 초기화 및 DB 스키마 업데이트 중...")

    try:
        # 1. 기존 presets 테이블 삭제 (DROP)
        # 주의: 기존 프리셋 데이터가 모두 날아갑니다.
        Preset.__table__.drop(engine)
        print("✅ 기존 Presets 테이블 삭제 완료")
    except Exception as e:
        print(f"⚠️ Presets 테이블 삭제 건너뜀 (없거나 오류): {e}")

    try:
        # 2. 모델 정의에 맞춰 모든 테이블 다시 생성 (CREATE)
        # 이 단계에서 custom_npcs 테이블이 없다면 자동으로 생성됩니다.
        Base.metadata.create_all(bind=engine)
        print("✅ 테이블 재생성 및 스키마 업데이트 완료 (Presets, CustomNPC 등)")
    except Exception as e:
        print(f"❌ 테이블 생성 실패: {e}")

if __name__ == "__main__":
    reset_presets_table()