import os
from models import engine, Preset, Base


def reset_presets_table():
    print("🔄 Presets 테이블 초기화 중...")

    try:
        # 1. 기존 presets 테이블 삭제 (DROP)
        Preset.__table__.drop(engine)
        print("✅ 기존 Presets 테이블 삭제 완료")
    except Exception as e:
        print(f"⚠️ 테이블 삭제 중 메시지 (무시 가능): {e}")

    try:
        # 2. 모델 정의에 맞춰 테이블 다시 생성 (CREATE)
        Base.metadata.create_all(bind=engine)
        print("✅ Presets 테이블 재생성 완료 (filename 컬럼 포함됨)")
    except Exception as e:
        print(f"❌ 테이블 생성 실패: {e}")


if __name__ == "__main__":
    reset_presets_table()