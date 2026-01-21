"""
비동기 S3 클라이언트 (MinIO/AWS S3 호환)
FastAPI 비동기 환경에 최적화된 aioboto3 기반 구현
"""
import os
import logging
from typing import Optional
import aioboto3
from botocore.exceptions import ClientError
from datetime import datetime
import uuid

logger = logging.getLogger(__name__)


class AsyncS3Client:
    """비동기 S3 클라이언트 (MinIO/AWS S3 호환)"""

    def __init__(self):
        self.endpoint = os.getenv("S3_ENDPOINT")
        self.access_key = os.getenv("S3_ACCESS_KEY")
        self.secret_key = os.getenv("S3_SECRET_KEY")
        # ✅ [작업 2] 버킷 이름을 무조건 소문자로 강제 변환
        bucket_raw = os.getenv("S3_BUCKET", "trpg-assets")
        self.bucket = bucket_raw.lower()
        self.region = os.getenv("S3_REGION", "us-east-1")

        # ✅ [작업 2] 내부망 통신을 위한 secure 설정 판단
        self.use_ssl = self.endpoint and self.endpoint.startswith("https://") if self.endpoint else True

        # 로컬 환경 배려: 환경변수 없으면 경고만 출력하고 None으로 설정
        self._is_configured = all([self.endpoint, self.access_key, self.secret_key])

        if not self._is_configured:
            logger.warning("⚠️ [S3] S3 환경변수가 설정되지 않았습니다. S3 기능이 비활성화됩니다.")
            logger.warning("   필요한 환경변수: S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET")
        else:
            logger.info(f"✅ [S3] S3 클라이언트 초기화 완료: {self.endpoint} / {self.bucket} (SSL: {self.use_ssl})")

        self._session = None
        self._initialized = False

    @property
    def is_available(self) -> bool:
        """S3 기능이 사용 가능한지 확인"""
        return self._is_configured

    async def initialize(self):
        """버킷 존재 확인 및 자동 생성"""
        if not self._is_configured:
            logger.warning("⚠️ [S3] S3가 구성되지 않아 초기화를 건너뜁니다.")
            return

        if self._initialized:
            return

        try:
            self._session = aioboto3.Session(
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region
            )

            # ✅ [작업 2] use_ssl 설정 적용
            async with self._session.client(
                's3',
                endpoint_url=self.endpoint,
                region_name=self.region,
                use_ssl=self.use_ssl
            ) as s3:
                try:
                    # 버킷 존재 확인
                    await s3.head_bucket(Bucket=self.bucket)
                    logger.info(f"✅ [S3] 버킷 확인 완료: {self.bucket}")
                except ClientError as e:
                    # ✅ [작업 2] HeadBucket 예외 세분화
                    error_code = e.response.get('Error', {}).get('Code', '')
                    http_status = e.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0)

                    logger.warning(f"⚠️ [S3] HeadBucket error: code={error_code}, status={http_status}")

                    if error_code == '404' or http_status == 404:
                        # 버킷이 없으면 생성
                        logger.info(f"📦 [S3] 버킷이 없어 생성합니다: {self.bucket}")
                        await s3.create_bucket(Bucket=self.bucket)
                        logger.info(f"✅ [S3] 버킷 생성 완료: {self.bucket}")
                    elif error_code == '400' or http_status == 400:
                        # ✅ [작업 2] 400 에러 처리 - 버킷 이름 문제일 수 있으므로 재시도하지 않고 경고만 출력
                        logger.error(f"❌ [S3] 버킷 이름 오류 (400 Bad Request): {self.bucket}")
                        logger.error(f"   버킷 이름은 소문자, 숫자, 하이픈만 사용 가능합니다.")
                        self._is_configured = False
                        return
                    else:
                        logger.error(f"❌ [S3] 버킷 확인 중 오류: {e}")
                        raise

            self._initialized = True

        except Exception as e:
            logger.error(f"❌ [S3] 초기화 실패: {e}")
            self._is_configured = False

    async def upload_file(
        self,
        file_data: bytes,
        filename: str,
        content_type: Optional[str] = None,
        folder: str = "uploads"
    ) -> Optional[str]:
        """
        파일을 S3에 업로드하고 접근 URL 반환

        Args:
            file_data: 업로드할 파일의 바이트 데이터
            filename: 원본 파일명
            content_type: MIME 타입 (예: 'image/png')
            folder: S3 내 폴더 경로

        Returns:
            업로드된 파일의 접근 URL (실패 시 None)
        """
        if not self._is_configured:
            logger.error("❌ [S3] S3가 구성되지 않아 업로드할 수 없습니다.")
            return None

        if not self._initialized:
            await self.initialize()

        try:
            # 고유한 파일명 생성 (충돌 방지)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            unique_id = str(uuid.uuid4())[:8]
            file_extension = os.path.splitext(filename)[1]
            unique_filename = f"{timestamp}_{unique_id}{file_extension}"

            # S3 키 생성 (폴더/파일명)
            s3_key = f"{folder}/{unique_filename}"

            # ✅ [작업 2] use_ssl 설정 적용
            async with self._session.client(
                's3',
                endpoint_url=self.endpoint,
                region_name=self.region,
                use_ssl=self.use_ssl
            ) as s3:
                # 업로드 파라미터
                upload_params = {
                    'Bucket': self.bucket,
                    'Key': s3_key,
                    'Body': file_data,
                }

                # Content-Type 설정 (있으면)
                if content_type:
                    upload_params['ContentType'] = content_type

                # 파일 업로드
                await s3.put_object(**upload_params)

                logger.info(f"✅ [S3] 파일 업로드 성공: {s3_key} ({len(file_data)} bytes)")

            # 접근 URL 생성
            # MinIO의 경우: {endpoint}/{bucket}/{key}
            # AWS S3의 경우: https://{bucket}.s3.{region}.amazonaws.com/{key}
            if "amazonaws.com" in self.endpoint:
                # AWS S3
                file_url = f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{s3_key}"
            else:
                # MinIO or custom S3
                file_url = f"{self.endpoint}/{self.bucket}/{s3_key}"

            return file_url

        except Exception as e:
            logger.error(f"❌ [S3] 파일 업로드 실패: {e}")
            return None

    async def delete_file(self, s3_key: str) -> bool:
        """
        S3에서 파일 삭제

        Args:
            s3_key: S3 객체 키 (예: 'uploads/20260115_abc123.png')

        Returns:
            삭제 성공 여부
        """
        if not self._is_configured:
            logger.error("❌ [S3] S3가 구성되지 않아 삭제할 수 없습니다.")
            return False

        if not self._initialized:
            await self.initialize()

        try:
            # ✅ [작업 2] use_ssl 설정 적용
            async with self._session.client(
                's3',
                endpoint_url=self.endpoint,
                region_name=self.region,
                use_ssl=self.use_ssl
            ) as s3:
                await s3.delete_object(Bucket=self.bucket, Key=s3_key)
                logger.info(f"✅ [S3] 파일 삭제 성공: {s3_key}")
                return True

        except Exception as e:
            logger.error(f"❌ [S3] 파일 삭제 실패: {e}")
            return False


# 싱글톤 인스턴스
_s3_client: Optional[AsyncS3Client] = None


def get_s3_client() -> AsyncS3Client:
    """S3 클라이언트 싱글톤 인스턴스 반환"""
    global _s3_client
    if _s3_client is None:
        _s3_client = AsyncS3Client()
    return _s3_client
