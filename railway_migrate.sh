#!/bin/bash
# Railway 배포 후 마이그레이션 실행 스크립트

#echo "🚀 Starting Railway Database Migration..."

# 마이그레이션 실행
#python migrate_db.py

# [수정] migrate_db.py 대신 init_db.py 실행 (테이블 생성 + 마이그레이션 통합)
#python init_db.py

#if [ $? -eq 0 ]; then
#    echo "✅ Migration completed successfully!"
#else
#    echo "❌ Migration failed!"
#    exit 1
#fi

#railway_migrate.sh 파일의 존재를 감지하고 자동으로 실행하려고 하는데, railway.json과
# Procfile에서 초기화 명령을 제거했음에도 불구하고, Railway의 **내부 빌더(Nixpacks)**가
# 이 쉘 스크립트를 우선적으로 실행하려 하기 때문입니다.
# Railway 자동 감지 방지용 더미 스크립트
# 실제 마이그레이션은 app.py의 lifespan에서 실행됩니다.

echo "⚠️  Skipping migration script (Handled by app.py)"
exit 0