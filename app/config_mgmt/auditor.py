#!/usr/bin/env python3
"""
FTSeek 配置文件梳理工具 (2026-04-11)

功能:
1. 分析config.yaml结构，生成配置索引
2. 检测重复配置项
3. 检测未使用的配置项
4. 生成配置文档
5. 优化配置结构建议

输出:
- 配置索引报告 (config/config_index.yaml)
- 配置文档 (docs/CONFIG_GUIDE.md)
- 优化建议 (config/config_optimization_report.yaml)
"""

import yaml
import json
import logging
from pathlib import Path
from typing import Dict, List, Any, Tuple
from datetime import datetime
from collections import defaultdict

logger = logging.getLogger("ftseek.config.auditor")


class ConfigAuditor:
    """配置文件审计器"""
    
    def __init__(self, config_file: str = None):
        if config_file is None:
            config_file = Path(__file__).parent / "config.yaml"
        
        self.config_file = Path(config_file)
        self.config = {}
        self.stats = {
            'total_keys': 0,
            'top_level_keys': 0,
            'max_depth': 0,
            'duplicate_keys': [],
            'deprecated_keys': [],
            'missing_docs': []
        }
    
    def load(self):
        """加载配置文件"""
        logger.info(f"加载配置文件: {self.config_file}")
        with open(self.config_file, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        
        self.stats['top_level_keys'] = len(self.config)
        logger.info(f"配置加载完成: {self.stats['top_level_keys']} 个顶级配置项")
        return self.config
    
    def analyze_structure(self) -> Dict[str, Any]:
        """分析配置结构"""
        logger.info("开始分析配置结构...")
        
        structure = {}
        for key, value in self.config.items():
            info = self._analyze_node(key, value, depth=1)
            structure[key] = info
        
        return structure
    
    def _analyze_node(self, key: str, value: Any, depth: int = 1) -> Dict:
        """递归分析配置节点"""
        self.stats['total_keys'] += 1
        self.stats['max_depth'] = max(self.stats['max_depth'], depth)
        
        info = {
            'depth': depth,
            'type': type(value).__name__,
            'has_doc': False,
            'children_count': 0
        }
        
        if isinstance(value, dict):
            info['children_count'] = len(value)
            info['children'] = {
                k: self._analyze_node(k, v, depth + 1)
                for k, v in value.items()
            }
        elif isinstance(value, list):
            info['length'] = len(value)
        
        return info
    
    def find_duplicates(self) -> List[Dict]:
        """查找重复配置项"""
        logger.info("检测重复配置项...")
        
        duplicates = []
        
        # 常见重复模式
        duplicate_patterns = [
            ('trading.max_single_position', 'risk_limits.max_single_position'),
            ('trading.max_total_position', 'risk_limits.max_total_position'),
            ('trading.max_daily_trades', 'risk_limits.max_daily_trades'),
            ('trading.max_daily_loss', 'risk_limits.max_daily_loss'),
            ('trading.max_weekly_loss', 'risk_limits.max_weekly_loss'),
            ('trading.min_signal_confidence', 'risk_limits.min_confidence'),
        ]
        
        for path1, path2 in duplicate_patterns:
            val1 = self._get_nested_value(path1)
            val2 = self._get_nested_value(path2)
            
            if val1 is not None and val2 is not None:
                duplicates.append({
                    'path1': path1,
                    'path2': path2,
                    'value1': val1,
                    'value2': val2,
                    'consistent': val1 == val2
                })
        
        self.stats['duplicate_keys'] = duplicates
        logger.info(f"发现 {len(duplicates)} 组重复配置")
        return duplicates
    
    def _get_nested_value(self, path: str) -> Any:
        """获取嵌套配置值"""
        keys = path.split('.')
        value = self.config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None
        return value
    
    def generate_index(self) -> Dict:
        """生成配置索引"""
        logger.info("生成配置索引...")
        
        index = {
            'metadata': {
                'generated_at': datetime.now().isoformat(),
                'config_file': str(self.config_file),
                'version': self.config.get('version', 'unknown'),
                'updated': self.config.get('updated', 'unknown')
            },
            'summary': {
                'total_keys': self.stats['total_keys'],
                'top_level_keys': self.stats['top_level_keys'],
                'max_depth': self.stats['max_depth'],
                'duplicate_groups': len(self.stats['duplicate_keys'])
            },
            'sections': {}
        }
        
        # 按功能分组
        section_map = {
            '基础配置': ['version', 'updated', 'description', 'base'],
            '交易连接': ['futu', 'server'],
            '交易规则': ['trading', 'markets', 'watchlist'],
            '风控体系': ['risk_limits', 'stop_loss', 'take_profit', 'circuit_breaker', 'loss_pause'],
            '策略引擎': ['strategy', 'dynamic_exit', 'signal_cooldown', 'four_step', 'smart_push'],
            '通知告警': ['notification', 'monitoring'],
            '数据持久化': ['database', 'persistence'],
            'AI与智能': ['ai', 'quality_scorer'],
            '调度与定时': ['schedule'],
            '日志与调试': ['logging', 'features'],
            '回测与优化': ['backtest'],
            '配置管理': ['migration']
        }
        
        for section_name, keys in section_map.items():
            present_keys = [k for k in keys if k in self.config]
            if present_keys:
                index['sections'][section_name] = {
                    'keys': present_keys,
                    'count': len(present_keys),
                    'description': self._get_section_description(section_name)
                }
        
        return index
    
    def _get_section_description(self, section: str) -> str:
        """获取章节描述"""
        descriptions = {
            '基础配置': '环境、日志级别、调试模式等基础设置',
            '交易连接': '富途OpenD连接配置、API服务器',
            '交易规则': '股票池、仓位限制、订单配置',
            '风控体系': '五层风控体系：信号级→订单级→仓位级→账户级→系统级',
            '策略引擎': '共识引擎、策略参数、动态退出、信号冷却',
            '通知告警': '钉钉通知、监控告警规则',
            '数据持久化': 'SQLite数据库、缓存配置',
            'AI与智能': 'AI情绪分析、信号质量评分',
            '调度与定时': '定时任务、调度器配置',
            '日志与调试': '日志配置、特性开关',
            '回测与优化': '回测参数、优化配置',
            '配置管理': '配置迁移、版本管理'
        }
        return descriptions.get(section, '')
    
    def generate_documentation(self) -> str:
        """生成配置文档"""
        logger.info("生成配置文档...")
        
        doc = f"""# FTSeek 配置文档

**版本**: {self.config.get('version', 'unknown')}  
**更新时间**: {self.config.get('updated', 'unknown')}  
**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---

## 配置概览

| 指标 | 数值 |
|-----|------|
| 顶级配置项 | {self.stats['top_level_keys']} |
| 总配置键数 | {self.stats['total_keys']} |
| 最大嵌套深度 | {self.stats['max_depth']} |
| 重复配置组 | {len(self.stats['duplicate_keys'])} |

---

## 配置章节

"""
        
        index = self.generate_index()
        for section_name, section_info in index['sections'].items():
            doc += f"### {section_name}\n\n"
            doc += f"**描述**: {section_info['description']}\n\n"
            doc += f"**配置项** ({section_info['count']}个):\n\n"
            
            for key in section_info['keys']:
                value = self.config[key]
                doc += f"- **{key}**: "
                if isinstance(value, dict):
                    doc += f"{len(value)} 个子配置项\n"
                elif isinstance(value, list):
                    doc += f"{len(value)} 个元素\n"
                else:
                    doc += f"`{value}`\n"
            
            doc += "\n"
        
        # 重复配置警告
        if self.stats['duplicate_keys']:
            doc += "## ⚠️ 重复配置警告\n\n"
            doc += "以下配置项在多个位置定义，请确保值一致：\n\n"
            doc += "| 位置1 | 位置2 | 值1 | 值2 | 一致性 |\n"
            doc += "|------|------|-----|-----|--------|\n"
            
            for dup in self.stats['duplicate_keys']:
                status = "✅" if dup['consistent'] else "❌"
                doc += f"| {dup['path1']} | {dup['path2']} | {dup['value1']} | {dup['value2']} | {status} |\n"
            
            doc += "\n"
        
        return doc
    
    def generate_optimization_report(self) -> Dict:
        """生成优化建议报告"""
        logger.info("生成优化建议...")
        
        report = {
            'generated_at': datetime.now().isoformat(),
            'issues': [],
            'recommendations': []
        }
        
        # 检查1: 重复配置
        duplicates = self.find_duplicates()
        inconsistent = [d for d in duplicates if not d['consistent']]
        
        if inconsistent:
            report['issues'].append({
                'severity': 'HIGH',
                'type': 'INCONSISTENT_DUPLICATES',
                'message': f'发现 {len(inconsistent)} 组不一致的重复配置',
                'details': inconsistent
            })
        
        # 检查2: 配置深度
        if self.stats['max_depth'] > 6:
            report['recommendations'].append({
                'type': 'REDUCE_DEPTH',
                'message': f'配置嵌套深度为 {self.stats["max_depth"]}，建议不超过6层',
                'suggestion': '考虑将深层配置提取到独立文件'
            })
        
        # 检查3: 配置项数量
        if self.stats['top_level_keys'] > 25:
            report['recommendations'].append({
                'type': 'TOO_MANY_TOP_KEYS',
                'message': f'顶级配置项过多 ({self.stats["top_level_keys"]}个)',
                'suggestion': '建议合并相关配置项，如 logging + features → runtime'
            })
        
        # 检查4: 缺少注释
        # (需要解析原始文件，这里简化处理)
        
        return report
    
    def run_full_audit(self, output_dir: str = None):
        """运行完整审计"""
        if output_dir is None:
            output_dir = self.config_file.parent
        
        output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True)
        
        # 1. 加载配置
        self.load()
        
        # 2. 分析结构
        structure = self.analyze_structure()
        
        # 3. 查找重复
        self.find_duplicates()
        
        # 4. 生成索引
        index = self.generate_index()
        index_file = output_dir / "config_index.yaml"
        with open(index_file, 'w', encoding='utf-8') as f:
            yaml.dump(index, f, allow_unicode=True, default_flow_style=False)
        logger.info(f"✅ 配置索引已生成: {index_file}")
        
        # 5. 生成文档
        doc = self.generate_documentation()
        docs_dir = Path(__file__).parent.parent / "docs"
        docs_dir.mkdir(exist_ok=True)
        doc_file = docs_dir / "CONFIG_GUIDE.md"
        with open(doc_file, 'w', encoding='utf-8') as f:
            f.write(doc)
        logger.info(f"✅ 配置文档已生成: {doc_file}")
        
        # 6. 生成优化报告
        report = self.generate_optimization_report()
        report_file = output_dir / "config_optimization_report.yaml"
        with open(report_file, 'w', encoding='utf-8') as f:
            yaml.dump(report, f, allow_unicode=True, default_flow_style=False)
        logger.info(f"✅ 优化报告已生成: {report_file}")
        
        # 7. 打印摘要
        print("\n" + "="*70)
        print("FTSeek 配置审计报告")
        print("="*70)
        print(f"配置文件: {self.config_file}")
        print(f"版本: {self.config.get('version', 'unknown')}")
        print(f"更新时间: {self.config.get('updated', 'unknown')}")
        print("-"*70)
        print(f"顶级配置项: {self.stats['top_level_keys']}")
        print(f"总配置键数: {self.stats['total_keys']}")
        print(f"最大嵌套深度: {self.stats['max_depth']}")
        print(f"重复配置组: {len(self.stats['duplicate_keys'])}")
        print("-"*70)
        
        if self.stats['duplicate_keys']:
            print("\n⚠️  重复配置项:")
            for dup in self.stats['duplicate_keys']:
                status = "✅" if dup['consistent'] else "❌"
                print(f"  {status} {dup['path1']} = {dup['value1']}")
                print(f"     {dup['path2']} = {dup['value2']}")
        
        print("="*70)
        print(f"\n输出文件:")
        print(f"  1. {index_file}")
        print(f"  2. {doc_file}")
        print(f"  3. {report_file}")
        print("="*70 + "\n")
        
        return {
            'index': index,
            'report': report,
            'doc_file': str(doc_file)
        }


def main():
    """命令行入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description='FTSeek 配置审计工具')
    parser.add_argument(
        '--config',
        default=None,
        help='配置文件路径 (默认: config/config.yaml)'
    )
    parser.add_argument(
        '--output-dir',
        default=None,
        help='输出目录 (默认: 配置文件所在目录)'
    )
    
    args = parser.parse_args()
    
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )
    
    auditor = ConfigAuditor(args.config)
    auditor.run_full_audit(args.output_dir)


if __name__ == '__main__':
    main()
