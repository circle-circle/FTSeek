"""
配置版本管理 - 自动备份与回滚支持
"""
import shutil
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class ConfigVersionManager:
    """配置文件版本管理器"""

    def __init__(self, config_dir: str = None, max_backups: int = 10):
        if config_dir is None:
            config_dir = str(Path(__file__).parent.parent.parent / 'config')
        self.config_dir = Path(config_dir)
        self.backup_dir = self.config_dir / 'backups'
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.max_backups = max_backups

    def backup_before_change(self, config_file: str, reason: str = "") -> str:
        """
        在修改配置前自动备份
        Returns: 备份文件路径
        """
        source = self.config_dir / config_file
        if not source.exists():
            # 也检查 app/risk/ 下
            alt_source = Path(__file__).parent.parent / 'risk' / config_file
            if alt_source.exists():
                source = alt_source
            else:
                logger.warning(f"Config file not found: {config_file}")
                return ""

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_name = f"{source.stem}_{timestamp}{source.suffix}"
        backup_path = self.backup_dir / backup_name

        shutil.copy2(source, backup_path)
        logger.info(f"Config backup: {source.name} -> {backup_path.name} (reason: {reason})")

        # 清理旧备份
        self._cleanup_old_backups(source.stem)

        return str(backup_path)

    def rollback(self, config_file: str, steps_back: int = 1) -> bool:
        """
        回滚配置到N步前的版本
        """
        stem = Path(config_file).stem
        backups = sorted(
            self.backup_dir.glob(f"{stem}_*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )

        if len(backups) < steps_back:
            logger.error(f"Not enough backups for {config_file} (have {len(backups)}, need {steps_back})")
            return False

        target_backup = backups[steps_back - 1]

        # 找到原始文件
        dest = self.config_dir / config_file
        if not dest.exists():
            dest = Path(__file__).parent.parent / 'risk' / config_file

        if not dest.exists():
            logger.error(f"Original config file not found: {config_file}")
            return False

        shutil.copy2(target_backup, dest)
        logger.info(f"Rollback: {config_file} <- {target_backup.name}")
        return True

    def list_backups(self, config_file: str) -> list:
        """列出配置文件的所有备份"""
        stem = Path(config_file).stem
        backups = sorted(
            self.backup_dir.glob(f"{stem}_*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        return [
            {
                'path': str(b),
                'name': b.name,
                'time': datetime.fromtimestamp(b.stat().st_mtime).isoformat(),
                'size': b.stat().st_size,
            }
            for b in backups
        ]

    def _cleanup_old_backups(self, stem: str):
        """清理超过上限的旧备份"""
        backups = sorted(
            self.backup_dir.glob(f"{stem}_*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        for old in backups[self.max_backups:]:
            old.unlink()
            logger.debug(f"Removed old backup: {old.name}")
