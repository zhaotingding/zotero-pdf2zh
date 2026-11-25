## server.py v4.0.0
# guaguastandup
# zotero-pdf2zh
import json, toml
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from utils.config_map import (
    pdf2zh_config_map,
    pdf2zh_next_config_map,
    pdf2zh_next_service_aliases,
)
from utils.deepseek_thinking import (
    is_deepseek_v4_model,
    normalize_deepseek_extra_data,
    remove_stale_thinking_fields,
    validate_winexe_runtime_if_selected,
)

pdf2zh = 'pdf2zh'
pdf2zh_next = 'pdf2zh_next'

_OPENAI_SEND_TEMPERATURE_ALIAS = 'openai_send_temperature'
_OPENAI_SEND_TEMPERATURE_UPSTREAM = 'openai_send_temprature'


def desktop_openai_key():
    """仅未选择 OpenAI 档案的 Windows 桌面任务使用既有专用凭据。"""
    import pywintypes
    import win32cred

    try:
        entry = win32cred.CredRead("Zotero-PDF2zh/OpenAI", win32cred.CRED_TYPE_GENERIC)
    except pywintypes.error as error:
        if error.winerror != 1168:
            raise
    else:
        key = entry["CredentialBlob"].decode("utf-16-le").strip()
        if key:
            return key
    raise ValueError(
        "请在 Zotero → PDF2zh → LLM API 配置管理中填写并激活 OpenAI 档案。"
    )


def _normalize_openai_send_temperature_key(values):
    """Map this project's old spelling to pdf2zh_next's compatibility key."""
    if not isinstance(values, dict):
        return
    if (
        _OPENAI_SEND_TEMPERATURE_ALIAS in values
        and _OPENAI_SEND_TEMPERATURE_UPSTREAM not in values
    ):
        values[_OPENAI_SEND_TEMPERATURE_UPSTREAM] = values[
            _OPENAI_SEND_TEMPERATURE_ALIAS
        ]
    values.pop(_OPENAI_SEND_TEMPERATURE_ALIAS, None)


def _safe_log_value(key, value):
    name = str(key or '').lower()
    if any(token in name for token in ('key', 'token', 'secret', 'password', 'auth', 'url', 'endpoint')):
        return '已设置' if value else ''
    return value


def stringToBoolean(value):
    if value == 'true' or value == 'True' or value == True or value == 1:
        return True
    return False


_TRUE_VALUES = {'true', '1', 'yes', 'on'}
_FALSE_VALUES = {'false', '0', 'no', 'off'}


def resolve_pdf2zh_next_service(service):
    return pdf2zh_next_service_aliases.get(service, service)


def _is_pdf2zh_next_bool_extra_key(key):
    name = str(key or '')
    return (
        name.endswith('_enable_json_mode')
        or name.endswith('_send_temprature')
        or name.endswith('_send_temperature')
        or name.endswith('_send_reasoning_effort')
        or name.endswith('_enable_thinking')
        or name.endswith('_send_enable_thinking_param')
    )


def coerce_pdf2zh_next_extra_value(key, value):
    """Keep extraData types compatible with pdf2zh_next pydantic fields.

    The plugin always sends extraData values as strings. toml needs native
    bool/int so fields like openai_enable_json_mode=false actually work.
    Empty values are skipped by the caller; False and 0 must still be written.
    """
    if isinstance(value, bool):
        return value
    if _is_pdf2zh_next_bool_extra_key(key):
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        text = str(value).strip().lower()
        if text in _TRUE_VALUES:
            return True
        if text in _FALSE_VALUES:
            return False
        return value
    if str(key) == 'num_predict':
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return value
    return value


class Config:
    def __init__(self, request_data):
        self.engine = request_data.get('engine', 'pdf2zh')
        if self.engine not in [pdf2zh, pdf2zh_next]:
            self.engine = pdf2zh

        if self.engine == pdf2zh:
            self.service = request_data.get('service', 'bing')
            if self.service in [None, ''] or len(self.service) < 3:
                self.service = 'bing'
        else:
            if not request_data.get('next_service') or request_data.get('next_service') in [None, '']:
                self.service = request_data.get('service', 'siliconflowfree')
            else:
                self.service = request_data.get('next_service', 'siliconflowfree')
            if self.service in [None, ''] or len(self.service) < 3:
                self.service = 'siliconflowfree'

        self.sourceLang = request_data.get('sourceLang', 'en')
        if self.sourceLang in [None, ''] or len(self.sourceLang) < 2:
            self.sourceLang = 'en'
        self.targetLang = request_data.get('targetLang', 'zh-CN')
        if self.targetLang in [None, ''] or len(self.targetLang) < 2:
            self.targetLang = 'zh-CN'

        self.skip_last_pages = request_data.get('skipLastPages', 0)
        try:
            self.skip_last_pages = int(self.skip_last_pages)
        except (ValueError, TypeError):
            self.skip_last_pages = 0
        if self.skip_last_pages < 0:
            self.skip_last_pages = 0

        self.thread_num = request_data.get('threadNum', 8)
        try: 
            self.thread_num = int(self.thread_num)
            if self.thread_num < 1:
                self.thread_num = 8
        except (ValueError, TypeError):
            self.thread_num = 8
        
        self.qps = request_data.get('qps', 4)
        try:
            self.qps = int(self.qps)
        except (ValueError, TypeError):
            self.qps = 4
        if self.qps < 1:
            self.qps = 4
        
        self.pool_size = request_data.get('poolSize', 0)
        try:
            self.pool_size = int(self.pool_size)
        except (ValueError, TypeError):
            self.pool_size = 0

        # pdf2zh_next uses qps as the worker count when pool_max_workers is unset.
        # Keep 0 as "unset/follow qps" instead of the legacy qps * 10 expansion.
        if self.pool_size < 0:
            self.pool_size = 0
        if self.pool_size > 1000:
            self.pool_size = 1000

        # 如果左右留白部分裁剪太多了, 可以调整pdf_w_offset和pdf_offset_ratio, 宽边裁剪值pdf_w_offset, 窄边裁剪值pdf_w_offset/pdf_offset_ratio
        # TODO: 将裁剪的逻辑添加到zotero配置页面
        self.pdf_w_offset = int(request_data.get('pdf_w_offset', 40))
        self.pdf_h_offset = int(request_data.get('pdf_h_offset', 20))
        self.pdf_offset_ratio = float(request_data.get('pdf_offset_ratio', 5))
        self.pdf_white_margin = int(request_data.get('pdf_white_margin', 0))

        self.mono = stringToBoolean(request_data.get('mono', True))
        self.dual = stringToBoolean(request_data.get('dual', True))
        self.mono_cut = stringToBoolean(request_data.get('mono_cut', False))
        self.dual_cut = stringToBoolean(request_data.get('dual_cut', False))
        self.crop_compare = stringToBoolean(request_data.get('crop_compare', False))
        self.compare = stringToBoolean(request_data.get('compare', False))
        # pdf2zh 1.x
        self.babeldoc = stringToBoolean(request_data.get('babeldoc', False))
        self.skip_font_subsets = stringToBoolean(request_data.get('skipSubsetFonts', False))
        self.font_file = request_data.get('fontFile', '') # pdf2zh 对应的字体路径
        # pdf2zh 2.x
        self.font_family = request_data.get('fontFamily', 'auto') # pdf2zh_next对应的字体选择
        self.dual_mode = request_data.get('dualMode', 'LR')
        self.trans_first = stringToBoolean(request_data.get('transFirst', False))
        self.ocr = stringToBoolean(request_data.get('ocr', False))
        self.auto_ocr = stringToBoolean(request_data.get('autoOcr', False))
        self.no_watermark = stringToBoolean(request_data.get('noWatermark', True))
        self.save_auto_extracted_glossary = stringToBoolean(request_data.get('saveGlossary', False))
        self.disable_glossary = stringToBoolean(request_data.get('disableGlossary', False))
        self.no_dual = stringToBoolean(request_data.get('noDual', False))
        self.no_mono = stringToBoolean(request_data.get('noMono', False))
        self.skip_clean = stringToBoolean(request_data.get('skipClean', False))
        self.enhance_compatibility = stringToBoolean(request_data.get('enhanceCompatibility', False))
        self.disable_rich_text_translate = stringToBoolean(request_data.get('disableRichTextTranslate', False))
        self.translate_table_text = stringToBoolean(request_data.get('translateTableText', False))
        self.only_include_translated_page = stringToBoolean(request_data.get('onlyIncludeTranslatedPage', False))

        print("\n🔍 Config without llm_api: ", self.__dict__)

        profile = request_data.get("llm_api") or {}
        self.has_api_profile = any(
            profile.get(field)
            for field in ("service", "apiKey", "apiUrl", "model", "extraData")
        )
        self.profile_service = profile.get("service", "")
        self.llm_api = {
            "apiKey": profile.get("apiKey", ""),
            "apiUrl": profile.get("apiUrl", ""),
            "model": profile.get("model", ""),
            "threadnum": profile.get("threadNum", self.thread_num),
            "extraData": dict(profile.get("extraData") or {}),
        }

    @property
    def desktop_fallback(self):
        return (
            not self.has_api_profile
            and self.service == "openai"
            and os.environ.get("ZOTERO_PDF2ZH_DESKTOP") == "1"
        )

    def _profile_defaults(self, current, mapping, service):
        if not (self.has_api_profile or self.desktop_fallback):
            return current
        if self.profile_service and resolve_pdf2zh_next_service(
            self.profile_service
        ) != resolve_pdf2zh_next_service(service):
            raise ValueError(
                "所选翻译服务与激活的 Zotero API 档案不一致，请重新选择档案。"
            )
        if self.has_api_profile and service in (
            "openai",
            "openailiked",
            "openaicompatible",
        ):
            if (self.llm_api["apiKey"] or "").strip() in ("", "null"):
                raise ValueError(
                    "当前激活的 Zotero API 档案缺少 API Key，请在设置中补填。"
                )
            if service != "openai" and not (self.llm_api["apiUrl"] or "").strip():
                raise ValueError(
                    "当前激活的兼容 API 档案缺少 Base URL，请在设置中补填。"
                )

        # 整个服务区段从随仓库的默认值重建，清空上一档案的地址、凭据及可选参数。
        templates = Path(__file__).resolve().parents[1] / "config"
        if self.engine == pdf2zh_next:
            detail = toml.load(templates / "config.toml.example").get(
                f"{service}_detail", {}
            )
        else:
            data = json.loads(
                (templates / "config.json.example").read_text(encoding="utf-8")
            )
            detail = next(
                (row["envs"] for row in data["translators"] if row["name"] == service),
                {},
            )
        if self.desktop_fallback:
            # 官方地址取自引擎默认值。仅复用已配置的模型，不把备用 Key 与旧网关配对。
            detail[mapping["apiKey"]] = desktop_openai_key()
            if current.get(mapping["model"]) not in (None, "", "null"):
                detail[mapping["model"]] = current[mapping["model"]]
            print(
                "API 配置来源：未选择 OpenAI 档案，使用 Windows 专用凭据和官方默认地址。"
            )
        else:
            print(f"API 配置来源：本次任务在 Zotero 激活的 {service} 档案。")
        return detail

    def engine_environment(self, environment):
        """引擎和 SDK 的环境设置优先于配置文件，只移除与本任务 API 档案有关的覆盖项。"""
        if not (self.has_api_profile or self.desktop_fallback):
            return environment
        service = (
            resolve_pdf2zh_next_service(self.service)
            if self.engine == pdf2zh_next
            else self.service
        )
        mappings = (
            pdf2zh_next_config_map if self.engine == pdf2zh_next else pdf2zh_config_map
        )
        mapping = mappings.get(service, {})
        fields = {mapping[k] for k in ("apiKey", "apiUrl", "model") if k in mapping}
        fields.update(mapping.get("extraData", ()))
        fields.update(self.llm_api["extraData"])
        prefix = "PDF2ZH_" if self.engine == pdf2zh_next else ""
        blocked = {prefix + key.upper() for key in fields}
        if service in (
            "openai",
            "openailiked",
            "openaicompatible",
            "azureopenai",
            "azure-openai",
        ):
            blocked.update(
                (
                    "OPENAI_API_KEY",
                    "OPENAI_ADMIN_KEY",
                    "OPENAI_BASE_URL",
                    "OPENAI_ORG_ID",
                    "OPENAI_PROJECT_ID",
                    "OPENAI_CUSTOM_HEADERS",
                    "PDF2ZH_OPENAI_SEND_TEMPERATURE",
                )
            )
        return {
            name: value
            for name, value in environment.items()
            if name.upper() not in blocked
        }

    @contextmanager
    def task_config(self, base, *, work_dir=None):
        # 已接受的任务独占配置直到引擎退出，成功和异常均清理；共享文件保留服务器设置。
        with TemporaryDirectory(prefix=".pdf2zh-api-", dir=work_dir) as directory:
            path = Path(directory) / Path(base).name
            self.update_config_file(base, output_file=path)
            yield str(path)

    def update_config_file(self, config_file, *, output_file=None):
        service = self.service
        engine = self.engine
        output_file = output_file or config_file
        if engine == pdf2zh:
            # 更新llm api config
            config_map = pdf2zh_config_map.get(service, {})
            with open(config_file, 'r', encoding='utf-8') as f:
                old_config = json.load(f)

            new_config = old_config.copy()

            # 更新字体
            if os.path.exists(self.font_file):
                new_config['NOTO_FONT_PATH'] = self.font_file
                print(f"✏️ 更新字体路径: {self.font_file}")

            if not config_map:
                # Font configuration is independent of translator credentials.
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(new_config, f, indent=4, ensure_ascii=False)
                return

            # 我们假设config.json文件的格式没有问题
            translator = None
            for t in new_config['translators']:
                if t.get('name') == service:
                    translator = t
                    break
            
            if translator is None:
                print(f"✏️ 服务 '{service}' 在先前配置中不存在, 创建新配置")
                translator = {'name': service, 'envs': {}}
                new_config['translators'].append(translator)
            else:
                if not isinstance(translator.get('envs'), dict): 
                    translator['envs'] = {}

            translator['envs'] = self._profile_defaults(translator['envs'], config_map, service)
            translator_keys = []
            if 'extraData' in config_map:
                for key in config_map['extraData']:
                    translator_keys.append(key)

            # 先对三个基本的参数进行映射, 如果存在映射关系, 则更新
            keys = ['apiKey', 'apiUrl', 'model'] 
            for key in keys:
                if key in self.llm_api and key in config_map:
                    value = self.llm_api[key]
                    mapped_key = config_map[key]
                    if value not in (None, "", [], {}):  # 跳过空值
                        translator['envs'][mapped_key] = value
                        translator_keys.append(mapped_key)
                        print(f"✏️ 更新 {key}: {mapped_key} = {_safe_log_value(key, value)}")
                    else:
                        translator_keys.append(mapped_key)
                        print(f"✏️ 跳过 {key}: {mapped_key} = {value} (empty or null)")

            # 将用户设置的extraData也进行映射, 如果存在映射关系, 则更新
            if 'extraData' in self.llm_api and isinstance(self.llm_api['extraData'], dict):
                identity_fields = {config_map[k] for k in keys if k in config_map}
                for key, value in self.llm_api['extraData'].items():
                    if key in identity_fields:
                        continue
                    if value not in (None, "", [], {}):
                        translator['envs'][key] = value
                        translator_keys.append(key)
                        print(f"✏️ 更新 extraData: {key} = {_safe_log_value(key, value)}")
                    else:
                        print(f"✏️ 跳过 extraData: {key} = {value} (empty or null)")

            # 将所有不在translator_keys中的key删除
            for key in list(translator['envs']):
                if key not in translator_keys:
                    del translator['envs'][key]
                    print(f"✏️ 删除旧 {key}")

            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(new_config, f, indent=4, ensure_ascii=False)
                print(f"✏️ 更新 config file: {output_file}")
            
        elif engine == pdf2zh_next: # toml文件, 格式参考server/config/config.toml.example
            service = resolve_pdf2zh_next_service(service)
            config_map = pdf2zh_next_config_map.get(service, {})
            with open(config_file, 'r', encoding='utf-8') as f:
                old_config = toml.load(f)
            # 每次生成独立快照；同一任务重试时也保留用户指定的并发数。
            old_config.setdefault('translation', {})['pool_max_workers'] = self.pool_size or 'null'
            old_config.setdefault('pdf', {})['only_include_translated_page'] = self.only_include_translated_page
            if not config_map:
                # 免费引擎等没有 API 区段，仍给本次 CLI 提供完整的配置文件。
                with open(output_file, 'w', encoding='utf-8') as f:
                    toml.dump(old_config, f)
                return
            detail_key = f'{service}_detail'
            old_config[detail_key] = self._profile_defaults(old_config.get(detail_key, {}), config_map, service)

            # A previous DeepSeek V4 attempt may have written 2.9-only fields.
            # For every non-DeepSeek request, scrub those fields before an older
            # runtime gets a chance to parse the shared config.toml.
            if service != 'deepseek':
                old_deepseek_detail = old_config.get('deepseek_detail')
                if isinstance(old_deepseek_detail, dict):
                    remove_stale_thinking_fields(old_deepseek_detail, '')

            # extraData is the generic plugin transport. DeepSeek V4 uses it to
            # carry the user's intent, but the server normalizes the values here
            # and execute.py later maps them to the exact upstream CLI flags.
            effective_deepseek_model = ''
            if service == 'deepseek':
                effective_deepseek_model = normalize_deepseek_extra_data(
                    self.llm_api,
                    old_config,
                )
                if is_deepseek_v4_model(effective_deepseek_model):
                    extra_data = self.llm_api.get("extraData") or {}
                    thinking_mode = extra_data.get(
                        "deepseek_thinking_mode", "disabled"
                    )
                    # winexe bypasses execute_with_progress(), so protect that
                    # execution path here. uv/conda/system runtimes are checked
                    # against the exact executable immediately before launch.
                    write_thinking_fields = validate_winexe_runtime_if_selected(
                        config_file,
                        effective_deepseek_model,
                        thinking_mode=thinking_mode,
                    )
                    if not write_thinking_fields:
                        extra_data.pop("deepseek_thinking_mode", None)
                        extra_data.pop("deepseek_reasoning_effort", None)
                        self.llm_api["extraData"] = extra_data

            new_config = old_config.copy() # 我们假设config.toml文件的格式没有问题

            translator = new_config[detail_key]

            if service == 'deepseek':
                remove_stale_thinking_fields(translator, effective_deepseek_model)
            elif service == 'openai':
                # Older zotero-pdf2zh releases exposed the correctly spelled
                # key, while pdf2zh_next intentionally retains the historical
                # "temprature" typo. Migrate both saved TOML and persisted
                # plugin extraData without affecting OpenAI-compatible services.
                _normalize_openai_send_temperature_key(translator)
                _normalize_openai_send_temperature_key(
                    self.llm_api.get('extraData')
                )
            
            translator_keys = ['translate_engine_type', 'support_llm']
            if 'extraData' in config_map:
                for key in config_map['extraData']:
                    translator_keys.append(key)

            keys = ['apiKey', 'apiUrl', 'model']
            for key in keys:
                if key in self.llm_api and key in config_map:
                    value = self.llm_api[key]
                    mapped_key = config_map[key]
                    if value not in (None, "", [], {}):
                        translator[mapped_key] = value
                        translator_keys.append(mapped_key)
                        print(f"✏️ 更新 {key}: {mapped_key} = {_safe_log_value(key, value)}")
                    else:
                        translator_keys.append(mapped_key)
                        print(f"✏️ 跳过 {key}: {mapped_key} = {value} (empty or null)")
            
            # 将用户设置的extraData也进行映射, 如果存在映射关系, 则更新
            if 'extraData' in self.llm_api and isinstance(self.llm_api['extraData'], dict):
                identity_fields = {config_map[k] for k in keys if k in config_map}
                for key, value in self.llm_api['extraData'].items():
                    if key in identity_fields:
                        continue
                    coerced = coerce_pdf2zh_next_extra_value(key, value)
                    if coerced not in (None, "", [], {}):
                        translator[key] = coerced
                        translator_keys.append(key)
                        print(f"✏️ 更新 extraData: {key} = {_safe_log_value(key, coerced)}")
                    else:
                        print(f"✏️ 跳过 extraData: {key} = {value} (empty or null)")

            # 将translator中, 所有不在translator_keys中的key删除
            print(translator.keys())
            for key in list(translator.keys()):
                if key not in translator_keys: 
                    del translator[key]
                    print(f"✏️ 删除旧 {key}")

            with open(output_file, 'w', encoding='utf-8') as f:
                toml.dump(new_config, f)
                print(f"✏️ 更新 config file: {output_file}")

        else:
            print(f"✏️ 不支持的引擎类型: {engine}")
