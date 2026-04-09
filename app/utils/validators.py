"""
数据验证工具类

提供输入参数验证、数据完整性检查、边界条件处理等功能

使用示例:
    from app.utils.validators import Validators, ValidationError
    
    # 验证股票代码
    Validators.validate_stock_code("HK.09992")
    
    # 验证价格
    Validators.validate_price(100.5, min_value=0.01, max_value=10000.0)
    
    # 验证订单数量
    Validators.validate_order_qty(100, lot_size=100, market="HK")
"""
import re
from typing import Any, Dict, List, Optional, Union, Callable
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from pathlib import Path


class ValidationError(Exception):
    """验证异常"""
    
    def __init__(self, field: str, message: str, value: Any = None):
        self.field = field
        self.message = message
        self.value = value
        super().__init__(f"{field}: {message} (value={value})")
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "field": self.field,
            "message": self.message,
            "value": self.value,
            "type": "ValidationError",
        }


class Validators:
    """验证工具类"""
    
    # ============================================================
    #  股票代码验证
    # ============================================================
    
    @staticmethod
    def validate_stock_code(code: str, market: Optional[str] = None) -> str:
        """
        验证股票代码格式
        
        Args:
            code: 股票代码
            market: 指定市场 (HK/US/CN)，None 表示自动检测
            
        Returns:
            标准化后的股票代码
            
        Raises:
            ValidationError: 验证失败
        """
        if not code or not isinstance(code, str):
            raise ValidationError("stock_code", "股票代码不能为空", code)
        
        code = code.strip().upper()
        
        # 港股格式：HK.XXXXX
        hk_pattern = r'^HK\.\d{5}$'
        # 美股格式：US.XXXXX
        us_pattern = r'^US\.[A-Z]{1,5}$'
        # A 股格式：SH.XXXXX / SZ.XXXXX
        cn_pattern = r'^(SH|SZ)\.\d{6}$'
        
        if market == "HK" or (market is None and re.match(hk_pattern, code)):
            if not re.match(hk_pattern, code):
                raise ValidationError(
                    "stock_code",
                    f"港股代码格式错误，应为 HK.XXXXX 格式",
                    code
                )
            return code
        
        elif market == "US" or (market is None and re.match(us_pattern, code)):
            if not re.match(us_pattern, code):
                raise ValidationError(
                    "stock_code",
                    f"美股代码格式错误，应为 US.XXXXX 格式",
                    code
                )
            return code
        
        elif market == "CN" or (market is None and re.match(cn_pattern, code)):
            if not re.match(cn_pattern, code):
                raise ValidationError(
                    "stock_code",
                    f"A 股代码格式错误，应为 SH.XXXXXX 或 SZ.XXXXXX 格式",
                    code
                )
            return code
        
        else:
            raise ValidationError(
                "stock_code",
                f"无法识别的股票代码格式，应为 HK.XXXXX、US.XXXXX 或 SH/SZ.XXXXXX",
                code
            )
    
    @staticmethod
    def validate_stock_codes(codes: List[str], market: Optional[str] = None) -> List[str]:
        """
        批量验证股票代码
        
        Args:
            codes: 股票代码列表
            market: 指定市场
            
        Returns:
            标准化后的股票代码列表
            
        Raises:
            ValidationError: 任一代码验证失败
        """
        if not codes or not isinstance(codes, list):
            raise ValidationError("stock_codes", "股票代码列表不能为空", codes)
        
        validated = []
        for i, code in enumerate(codes):
            try:
                validated.append(Validators.validate_stock_code(code, market))
            except ValidationError as e:
                raise ValidationError(f"stock_codes[{i}]", e.message, code)
        
        return validated
    
    # ============================================================
    #  价格验证
    # ============================================================
    
    @staticmethod
    def validate_price(
        price: Any,
        min_value: float = 0.01,
        max_value: float = 1000000.0,
        allow_zero: bool = False
    ) -> float:
        """
        验证价格
        
        Args:
            price: 价格值
            min_value: 最小值
            max_value: 最大值
            allow_zero: 是否允许 0 值
            
        Returns:
            验证后的价格 (float)
            
        Raises:
            ValidationError: 验证失败
        """
        try:
            price_float = float(price)
        except (TypeError, ValueError):
            raise ValidationError("price", "价格必须是数字", price)
        
        if price_float < 0:
            raise ValidationError("price", "价格不能为负数", price)
        
        if not allow_zero and price_float == 0:
            raise ValidationError("price", "价格不能为 0", price)
        
        if price_float < min_value:
            raise ValidationError(
                "price",
                f"价格不能小于 {min_value}",
                price
            )
        
        if price_float > max_value:
            raise ValidationError(
                "price",
                f"价格不能大于 {max_value}",
                price
            )
        
        return price_float
    
    @staticmethod
    def validate_price_change(
        current_price: float,
        reference_price: float,
        max_change_pct: float = 0.5
    ) -> float:
        """
        验证价格变动幅度
        
        Args:
            current_price: 当前价格
            reference_price: 参考价格
            max_change_pct: 最大允许变动比例 (0.5 表示 50%)
            
        Returns:
            变动比例
            
        Raises:
            ValidationError: 变动幅度过大
        """
        if reference_price <= 0:
            raise ValidationError("reference_price", "参考价格必须大于 0", reference_price)
        
        change_pct = abs(current_price - reference_price) / reference_price
        
        if change_pct > max_change_pct:
            raise ValidationError(
                "price_change",
                f"价格变动幅度过大：{change_pct:.2%} > {max_change_pct:.2%}",
                {"current": current_price, "reference": reference_price}
            )
        
        return change_pct
    
    # ============================================================
    #  订单数量验证
    # ============================================================
    
    @staticmethod
    def validate_order_qty(
        qty: Any,
        lot_size: int = 100,
        market: str = "HK",
        min_qty: Optional[int] = None,
        max_qty: Optional[int] = None
    ) -> int:
        """
        验证订单数量
        
        Args:
            qty: 数量
            lot_size: 每手股数
            market: 市场 (HK/US)
            min_qty: 最小数量
            max_qty: 最大数量
            
        Returns:
            验证后的数量 (int)
            
        Raises:
            ValidationError: 验证失败
        """
        try:
            qty_int = int(qty)
        except (TypeError, ValueError):
            raise ValidationError("order_qty", "数量必须是整数", qty)
        
        if qty_int <= 0:
            raise ValidationError("order_qty", "数量必须大于 0", qty)
        
        # 港股必须是整手倍数
        if market == "HK" and qty_int % lot_size != 0:
            raise ValidationError(
                "order_qty",
                f"港股数量必须是 {lot_size} 的倍数",
                qty
            )
        
        if min_qty is not None and qty_int < min_qty:
            raise ValidationError(
                "order_qty",
                f"数量不能小于 {min_qty}",
                qty
            )
        
        if max_qty is not None and qty_int > max_qty:
            raise ValidationError(
                "order_qty",
                f"数量不能大于 {max_qty}",
                qty
            )
        
        return qty_int
    
    # ============================================================
    #  金额验证
    # ============================================================
    
    @staticmethod
    def validate_amount(
        amount: Any,
        min_value: float = 0.01,
        max_value: float = 10000000.0,
        currency: str = "HKD"
    ) -> float:
        """
        验证交易金额
        
        Args:
            amount: 金额
            min_value: 最小值
            max_value: 最大值
            currency: 货币类型
            
        Returns:
            验证后的金额
            
        Raises:
            ValidationError: 验证失败
        """
        try:
            amount_float = float(amount)
        except (TypeError, ValueError):
            raise ValidationError("amount", "金额必须是数字", amount)
        
        if amount_float < 0:
            raise ValidationError("amount", "金额不能为负数", amount)
        
        if amount_float < min_value:
            raise ValidationError(
                "amount",
                f"金额不能小于 {min_value} {currency}",
                amount
            )
        
        if amount_float > max_value:
            raise ValidationError(
                "amount",
                f"金额不能大于 {max_value} {currency}",
                amount
            )
        
        return amount_float
    
    # ============================================================
    #  百分比验证
    # ============================================================
    
    @staticmethod
    def validate_percentage(
        value: Any,
        min_value: float = 0.0,
        max_value: float = 100.0,
        allow_negative: bool = False
    ) -> float:
        """
        验证百分比值
        
        Args:
            value: 百分比值
            min_value: 最小值
            max_value: 最大值
            allow_negative: 是否允许负值
            
        Returns:
            验证后的百分比值
            
        Raises:
            ValidationError: 验证失败
        """
        try:
            value_float = float(value)
        except (TypeError, ValueError):
            raise ValidationError("percentage", "百分比必须是数字", value)
        
        if not allow_negative and value_float < 0:
            raise ValidationError("percentage", "百分比不能为负数", value)
        
        if value_float < min_value:
            raise ValidationError(
                "percentage",
                f"百分比不能小于 {min_value}%",
                value
            )
        
        if value_float > max_value:
            raise ValidationError(
                "percentage",
                f"百分比不能大于 {max_value}%",
                value
            )
        
        return value_float
    
    # ============================================================
    #  时间验证
    # ============================================================
    
    @staticmethod
    def validate_datetime(
        dt: Any,
        min_date: Optional[datetime] = None,
        max_date: Optional[datetime] = None,
        allow_future: bool = True
    ) -> datetime:
        """
        验证日期时间
        
        Args:
            dt: 日期时间
            min_date: 最小日期
            max_date: 最大日期
            allow_future: 是否允许未来时间
            
        Returns:
            验证后的日期时间
            
        Raises:
            ValidationError: 验证失败
        """
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
            except ValueError:
                raise ValidationError("datetime", "日期时间格式错误", dt)
        
        if not isinstance(dt, (datetime, date)):
            raise ValidationError("datetime", "必须是日期时间类型", dt)
        
        if isinstance(dt, date) and not isinstance(dt, datetime):
            dt = datetime.combine(dt, datetime.min.time())
        
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        
        if not allow_future and dt > now:
            raise ValidationError(
                "datetime",
                "不允许未来时间",
                dt
            )
        
        if min_date and dt < min_date:
            raise ValidationError(
                "datetime",
                f"日期不能早于 {min_date}",
                dt
            )
        
        if max_date and dt > max_date:
            raise ValidationError(
                "datetime",
                f"日期不能晚于 {max_date}",
                dt
            )
        
        return dt
    
    # ============================================================
    #  策略配置验证
    # ============================================================
    
    @staticmethod
    def validate_strategy_config(config: Dict[str, Any]) -> Dict[str, str]:
        """
        验证策略配置
        
        Args:
            config: 策略配置字典
            
        Returns:
            错误消息字典，空字典表示配置有效
        """
        errors = {}
        
        if not isinstance(config, dict):
            return {"config": "配置必须是字典类型"}
        
        # 必需字段
        required_fields = ["enabled"]
        for field in required_fields:
            if field not in config:
                errors[field] = f"必需字段 '{field}' 缺失"
        
        # enabled 必须是布尔值
        if "enabled" in config and not isinstance(config["enabled"], bool):
            errors["enabled"] = "enabled 必须是布尔值"
        
        # 数值字段验证
        numeric_fields = ["period", "threshold", "ratio", "weight"]
        for field in numeric_fields:
            if field in config:
                try:
                    float(config[field])
                except (TypeError, ValueError):
                    errors[field] = f"{field} 必须是数字"
        
        return errors
    
    # ============================================================
    #  API 请求验证
    # ============================================================
    
    @staticmethod
    def validate_api_token(token: str, min_length: int = 16) -> str:
        """
        验证 API Token
        
        Args:
            token: Token 字符串
            min_length: 最小长度
            
        Returns:
            验证后的 Token
            
        Raises:
            ValidationError: 验证失败
        """
        if not token or not isinstance(token, str):
            raise ValidationError("api_token", "API Token 不能为空", token)
        
        token = token.strip()
        
        if len(token) < min_length:
            raise ValidationError(
                "api_token",
                f"API Token 长度不能小于 {min_length}",
                f"{token[:4]}...{len(token)}"
            )
        
        # 检查是否包含不安全字符
        if re.search(r'[\s\'";\\]', token):
            raise ValidationError(
                "api_token",
                "API Token 包含不安全字符",
                f"{token[:4]}..."
            )
        
        return token
    
    @staticmethod
    def validate_webhook_url(url: str) -> str:
        """
        验证 Webhook URL
        
        Args:
            url: Webhook URL
            
        Returns:
            验证后的 URL
            
        Raises:
            ValidationError: 验证失败
        """
        if not url or not isinstance(url, str):
            raise ValidationError("webhook_url", "Webhook URL 不能为空", url)
        
        url = url.strip()
        
        # 必须是 HTTPS
        if not url.startswith("https://"):
            raise ValidationError(
                "webhook_url",
                "Webhook URL 必须使用 HTTPS 协议",
                url
            )
        
        # 基本 URL 格式验证
        url_pattern = r'^https://[^\s/$.?#].[^\s]*$'
        if not re.match(url_pattern, url):
            raise ValidationError(
                "webhook_url",
                "Webhook URL 格式错误",
                url
            )
        
        return url
    
    # ============================================================
    #  数据完整性验证
    # ============================================================
    
    @staticmethod
    def validate_quote_data(quote: Dict[str, Any], code: str) -> Dict[str, str]:
        """
        验证报价数据完整性
        
        Args:
            quote: 报价数据字典
            code: 股票代码
            
        Returns:
            错误消息字典，空字典表示数据有效
        """
        errors = {}
        
        if not isinstance(quote, dict):
            return {"quote": "报价数据必须是字典类型"}
        
        # 必需字段
        required_fields = ["code", "price", "timestamp"]
        for field in required_fields:
            if field not in quote:
                errors[field] = f"必需字段 '{field}' 缺失"
        
        # 代码匹配
        if "code" in quote and quote["code"] != code:
            errors["code"] = f"代码不匹配：{quote['code']} != {code}"
        
        # 价格验证
        if "price" in quote:
            try:
                Validators.validate_price(quote["price"])
            except ValidationError as e:
                errors["price"] = e.message
        
        # 时间戳验证
        if "timestamp" in quote:
            try:
                Validators.validate_datetime(quote["timestamp"], allow_future=True)
            except ValidationError as e:
                errors["timestamp"] = e.message
        
        return errors
    
    @staticmethod
    def validate_kline_data(kline: Dict[str, Any]) -> Dict[str, str]:
        """
        验证 K 线数据完整性
        
        Args:
            kline: K 线数据字典
            
        Returns:
            错误消息字典，空字典表示数据有效
        """
        errors = {}
        
        if not isinstance(kline, dict):
            return {"kline": "K 线数据必须是字典类型"}
        
        # 必需字段
        required_fields = ["code", "time_key", "open", "high", "low", "close", "volume"]
        for field in required_fields:
            if field not in kline:
                errors[field] = f"必需字段 '{field}' 缺失"
        
        # OHLC 验证
        ohlc_fields = ["open", "high", "low", "close"]
        for field in ohlc_fields:
            if field in kline:
                try:
                    Validators.validate_price(kline[field])
                except ValidationError as e:
                    errors[field] = e.message
        
        # 高低点逻辑验证
        if "high" in kline and "low" in kline:
            if kline["high"] < kline["low"]:
                errors["high_low"] = "最高价不能低于最低价"
        
        if "high" in kline and "open" in kline and "close" in kline:
            if kline["high"] < max(kline["open"], kline["close"]):
                errors["high"] = "最高价应包含开盘价和收盘价"
        
        if "low" in kline and "open" in kline and "close" in kline:
            if kline["low"] > min(kline["open"], kline["close"]):
                errors["low"] = "最低价应包含开盘价和收盘价"
        
        # 成交量验证
        if "volume" in kline:
            try:
                vol = int(kline["volume"])
                if vol < 0:
                    errors["volume"] = "成交量不能为负数"
            except (TypeError, ValueError):
                errors["volume"] = "成交量必须是整数"
        
        return errors
    
    # ============================================================
    #  边界条件检查
    # ============================================================
    
    @staticmethod
    def check_boundary(
        value: float,
        lower_bound: float,
        upper_bound: float,
        field_name: str = "value",
        inclusive: bool = True
    ) -> bool:
        """
        检查值是否在边界范围内
        
        Args:
            value: 要检查的值
            lower_bound: 下界
            upper_bound: 上界
            field_name: 字段名称
            inclusive: 是否包含边界
            
        Returns:
            是否在范围内
            
        Raises:
            ValidationError: 超出边界
        """
        if lower_bound > upper_bound:
            raise ValidationError(
                "bounds",
                f"下界 ({lower_bound}) 不能大于上界 ({upper_bound})",
                {"lower": lower_bound, "upper": upper_bound}
            )
        
        try:
            value_float = float(value)
        except (TypeError, ValueError):
            raise ValidationError(field_name, "值必须是数字", value)
        
        if inclusive:
            if value_float < lower_bound or value_float > upper_bound:
                raise ValidationError(
                    field_name,
                    f"值超出范围 [{lower_bound}, {upper_bound}]",
                    value
                )
        else:
            if value_float <= lower_bound or value_float >= upper_bound:
                raise ValidationError(
                    field_name,
                    f"值超出范围 ({lower_bound}, {upper_bound})",
                    value
                )
        
        return True
    
    @staticmethod
    def check_not_none(value: Any, field_name: str) -> Any:
        """
        检查值不为 None
        
        Args:
            value: 要检查的值
            field_name: 字段名称
            
        Returns:
            原值
            
        Raises:
            ValidationError: 值为 None
        """
        if value is None:
            raise ValidationError(field_name, "值不能为 None", value)
        return value
    
    @staticmethod
    def check_not_empty(value: Any, field_name: str) -> Any:
        """
        检查值不为空
        
        Args:
            value: 要检查的值
            field_name: 字段名称
            
        Returns:
            原值
            
        Raises:
            ValidationError: 值为空
        """
        if value is None:
            raise ValidationError(field_name, "值不能为空", value)
        
        if isinstance(value, (str, list, dict, tuple, set)) and len(value) == 0:
            raise ValidationError(field_name, "值不能为空", value)
        
        return value
    
    # ============================================================
    #  文件路径验证
    # ============================================================
    
    @staticmethod
    def validate_path(
        path: Any,
        must_exist: bool = False,
        must_be_dir: bool = False,
        must_be_file: bool = False,
        check_writable: bool = False
    ) -> Path:
        """
        验证文件路径
        
        Args:
            path: 路径
            must_exist: 是否必须存在
            must_be_dir: 是否必须是目录
            must_be_file: 是否必须是文件
            check_writable: 是否检查可写权限
            
        Returns:
            Path 对象
            
        Raises:
            ValidationError: 验证失败
        """
        if not path or not isinstance(path, (str, Path)):
            raise ValidationError("path", "路径必须是字符串或 Path 对象", path)
        
        path_obj = Path(path)
        
        if must_exist and not path_obj.exists():
            raise ValidationError(
                "path",
                f"路径不存在",
                str(path_obj)
            )
        
        if must_be_dir and not path_obj.is_dir():
            raise ValidationError(
                "path",
                f"路径必须是目录",
                str(path_obj)
            )
        
        if must_be_file and not path_obj.is_file():
            raise ValidationError(
                "path",
                f"路径必须是文件",
                str(path_obj)
            )
        
        if check_writable:
            if path_obj.exists():
                if not os.access(path_obj, os.W_OK):
                    raise ValidationError(
                        "path",
                        f"路径不可写",
                        str(path_obj)
                    )
            else:
                # 检查父目录是否可写
                parent = path_obj.parent
                if parent.exists() and not os.access(parent, os.W_OK):
                    raise ValidationError(
                        "path",
                        f"父目录不可写",
                        str(path_obj)
                    )
        
        return path_obj
    
    # ============================================================
    #  组合验证器
    # ============================================================
    
    @staticmethod
    def validate_signal(
        signal: Dict[str, Any],
        required_fields: Optional[List[str]] = None
    ) -> Dict[str, str]:
        """
        验证交易信号
        
        Args:
            signal: 信号数据
            required_fields: 必需字段列表
            
        Returns:
            错误消息字典，空字典表示信号有效
        """
        errors = {}
        
        if not isinstance(signal, dict):
            return {"signal": "信号数据必须是字典类型"}
        
        # 默认必需字段
        if required_fields is None:
            required_fields = ["code", "action", "price", "timestamp"]
        
        # 检查必需字段
        for field in required_fields:
            if field not in signal:
                errors[field] = f"必需字段 '{field}' 缺失"
        
        # 验证股票代码
        if "code" in signal:
            try:
                Validators.validate_stock_code(signal["code"])
            except ValidationError as e:
                errors["code"] = e.message
        
        # 验证动作
        if "action" in signal:
            if signal["action"] not in ["BUY", "SELL", "HOLD"]:
                errors["action"] = f"动作必须是 BUY/SELL/HOLD，实际：{signal['action']}"
        
        # 验证价格
        if "price" in signal:
            try:
                Validators.validate_price(signal["price"])
            except ValidationError as e:
                errors["price"] = e.message
        
        # 验证数量 (如果有)
        if "qty" in signal:
            try:
                Validators.validate_order_qty(signal["qty"])
            except ValidationError as e:
                errors["qty"] = e.message
        
        # 验证时间戳
        if "timestamp" in signal:
            try:
                Validators.validate_datetime(signal["timestamp"], allow_future=True)
            except ValidationError as e:
                errors["timestamp"] = e.message
        
        return errors


# 导入 os 用于权限检查
import os
