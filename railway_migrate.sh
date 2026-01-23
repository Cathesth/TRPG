#!/bin/bash
# Railway 배포 후 마이그레이션 실행 스크립트

echo "🚀 Starting Railway Database Migration..."

# 마이그레이션 실행
#python migrate_db.py

# [수정] migrate_db.py 대신 init_db.py 실행 (테이블 생성 + 마이그레이션 통합)
python init_db.py

if [ $? -eq 0 ]; then
    echo "✅ Migration completed successfully!"
else
    echo "❌ Migration failed!"
    exit 1
fi

