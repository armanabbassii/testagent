"""
src/testcase_box/profiles.py — نیتِ میزبان و پروفایلِ اجرا برای آماده‌سازی (M4)

    profile = ExecutionProfile("staging", resolved_variables={"base_url", "userId"})
    result = prepare_case(version, host_intent=HostIntent.NORMAL, profile=profile, ...)

HostIntent را فراخواننده صریحاً می‌دهد — VDI هرگز از نامِ میزبان یا مسیر حدس زده نمی‌شود:
NORMAL → {{base_url}} و VDI → {{base_url_vdi}}.

ExecutionProfile فقط نام دارد، هرگز مقدار. سازنده‌ی پروفایل (مرحله‌ی اجرا، نه M4) باید
برای هر نام «مقدارِ نهایی» را با همان اولویتِ preflight ِ newman حل کند — environment،
سپس متغیرهای collection، سپس globals — و نام را فقط در یکی از دو مجموعه بگذارد:
  * resolved_variables — مقدارِ نهایی غیرِ خالی است؛
  * empty_variables   — نام تعریف شده ولی مقدارِ نهایی‌اش خالی است.
اجتماعِ ساده‌ی نام‌های سه منبع کافی نیست: مقدارِ خالیِ environment مقدارِ globals را
می‌پوشاند. نامی که در هیچ‌کدام نیست یعنی «حل نمی‌شود».

متغیرِ میزبانِ انتخاب‌شده هم مثلِ بقیه از همین دو مجموعه خوانده می‌شود و باید در
resolved_variables باشد (preflight مقدارِ غیرِ خالی می‌خواهد). «token» رزرو است: runtime
آن را همیشه به‌صورتِ envVar تزریق می‌کند و بر فایل‌ها مقدم است، پس ورودیِ پروفایل برای آن
نادیده گرفته و با یافته‌ی INFO گزارش می‌شود. متغیرهایی که caseِ دیگری تولید می‌کند در M4
حل نمی‌شوند — فقط اگر برنامه‌ریزِ اجرا آن‌ها را صریحاً در پروفایل بگذارد.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.testcase_box.contract import BASE_URL, BASE_URL_VDI, TOKEN_VARIABLE


class HostIntent(StrEnum):
    NORMAL = "NORMAL"
    VDI = "VDI"


HOST_VARIABLE_FOR_INTENT = {HostIntent.NORMAL: BASE_URL, HostIntent.VDI: BASE_URL_VDI}

# آینه‌ی postman-collection/lib/superstring/dynamic-variables.js (۱۱۷ نام). {{$name}} ِ
# ناشناخته را Postman تولید نمی‌کند و به همان شکلِ literal ارسال می‌شود. تست هم‌خوانی
# با node_modules را بررسی می‌کند — با ارتقای postman-collection به‌روز شود.
DYNAMIC_VARIABLES = frozenset(
    """
    $guid $timestamp $isoTimestamp $randomInt $randomPhoneNumber $randomPhoneNumberExt
    $randomLocale $randomWords $randomFilePath $randomDirectoryPath $randomCity
    $randomStreetName $randomStreetAddress $randomCountry $randomCountryCode $randomLatitude
    $randomLongitude $randomColor $randomDepartment $randomProductName $randomProductAdjective
    $randomProductMaterial $randomProduct $randomCompanyName $randomCompanySuffix
    $randomCatchPhrase $randomBs $randomCatchPhraseAdjective $randomCatchPhraseDescriptor
    $randomCatchPhraseNoun $randomBsAdjective $randomBsBuzz $randomBsNoun $randomDatabaseColumn
    $randomDatabaseType $randomDatabaseCollation $randomDatabaseEngine $randomDatePast
    $randomDateFuture $randomDateRecent $randomMonth $randomWeekday $randomBankAccount
    $randomBankAccountName $randomCreditCardMask $randomPrice $randomTransactionType
    $randomCurrencyCode $randomCurrencyName $randomCurrencySymbol $randomBitcoin
    $randomBankAccountIban $randomBankAccountBic $randomAbbreviation $randomAdjective
    $randomNoun $randomVerb $randomIngverb $randomPhrase $randomAvatarImage $randomImageUrl
    $randomAbstractImage $randomAnimalsImage $randomBusinessImage $randomCatsImage
    $randomCityImage $randomFoodImage $randomNightlifeImage $randomFashionImage
    $randomPeopleImage $randomNatureImage $randomSportsImage $randomTransportImage
    $randomImageDataUri $randomEmail $randomExampleEmail $randomUserName $randomProtocol
    $randomUrl $randomDomainName $randomDomainSuffix $randomDomainWord $randomIP
    $randomUserAgent $randomHexColor $randomMACAddress $randomPassword $randomLoremWord
    $randomLoremWords $randomLoremSentence $randomLoremSlug $randomLoremSentences
    $randomLoremParagraph $randomLoremParagraphs $randomLoremText $randomLoremLines
    $randomFirstName $randomLastName $randomFullName $randomJobTitle $randomNamePrefix
    $randomNameSuffix $randomJobDescriptor $randomJobArea $randomJobType $randomUUID
    $randomBoolean $randomWord $randomAlphaNumeric $randomFileName $randomCommonFileName
    $randomMimeType $randomCommonFileType $randomCommonFileExt $randomFileType $randomFileExt
    $randomSemver
    """.split()
)


def _names(field: str, values: object) -> frozenset[str]:
    if isinstance(values, str | bytes | dict) or not hasattr(values, "__iter__"):
        raise ValueError(f"{field} must be a collection of variable names.")
    names = frozenset(values)
    for name in names:
        # نامی که «{» یا «}» دارد با الگوی ارجاعِ Postman هرگز تطبیق نمی‌خورد
        if not isinstance(name, str) or not name.strip() or "{" in name or "}" in name:
            raise ValueError(f"{field} contains an invalid variable name.")
    return names


@dataclass(frozen=True)
class ExecutionProfile:
    """نام‌های متغیر با «حلِ نهایی» (بدونِ مقدار). مقدارها هرگز به M4 نمی‌رسند."""

    profile_id: str
    resolved_variables: frozenset[str] = frozenset()
    empty_variables: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ValueError("profile_id must be a non-empty string.")
        resolved = _names("resolved_variables", self.resolved_variables)
        empty = _names("empty_variables", self.empty_variables)
        if resolved & empty:
            raise ValueError("A variable cannot be both resolved and empty.")
        object.__setattr__(self, "resolved_variables", resolved)
        object.__setattr__(self, "empty_variables", empty)


def _scope(document: object, label: str) -> dict[str, object]:
    """ورودی‌های enabled ِ یک environment/globals ِ Postman — آخرین تعریف برنده (مثلِ
    preflight ِ newman). فقط ساختار بررسی می‌شود؛ مقدار هرگز در پیامِ خطا نمی‌آید."""
    if not isinstance(document, dict) or not isinstance(document.get("values"), list):
        raise ValueError(f"The {label} must be a Postman document with a 'values' array.")
    entries: dict[str, object] = {}
    for entry in document["values"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            continue
        if entry.get("enabled", True) is False:
            continue
        entries[entry["key"]] = entry.get("value")
    return entries


def variable_profile_from_documents(
    profile_id: str, environment: object, globals_document: object
) -> ExecutionProfile:
    """ExecutionProfile از environment و globals ِ از پیش parse‌شده (خالص — هیچ فایلی
    خوانده نمی‌شود؛ فراخواننده محتوا را می‌دهد و مقدارها از این تابع بیرون نمی‌روند).

    اولویتِ preflight ِ newman: environment بر globals مقدم است (کالکشنِ ساخته‌شده‌ی Box متغیر
    ندارد). مقدارِ مؤثرِ None یا رشته‌ی سفید → empty_variables؛ هر مقدارِ دیگر →
    resolved_variables. «token» کنار گذاشته می‌شود (runtime تزریقش می‌کند) و نامی که شکلِ
    ارجاعِ Postman را نمی‌تواند داشته باشد (خالی یا دارای «{» / «}») نادیده گرفته می‌شود.
    """
    effective = {**_scope(globals_document, "globals"), **_scope(environment, "environment")}
    resolved, empty = set(), set()
    for name, value in effective.items():
        if name == TOKEN_VARIABLE or not name.strip() or "{" in name or "}" in name:
            continue
        blank = value is None or (isinstance(value, str) and not value.strip())
        (empty if blank else resolved).add(name)
    return ExecutionProfile(profile_id, frozenset(resolved), frozenset(empty))


def read_variable_profile(profile_id: str, environment_path, globals_path) -> ExecutionProfile:
    """همان variable_profile_from_documents از روی فایل‌ها (M7.6) — مقدارها فقط در حافظه‌ی همین
    تابع‌اند و هرگز برگردانده یا در پیام آورده نمی‌شوند. فایلِ ناخوانا یا نامعتبر → ValueError."""
    import json
    from pathlib import Path

    documents = []
    for label, path in (("environment", environment_path), ("globals", globals_path)):
        try:
            documents.append(json.loads(Path(path).read_bytes().decode("utf-8-sig")))
        except (OSError, UnicodeDecodeError, ValueError):
            raise ValueError(f"The Postman {label} file cannot be read.") from None
    return variable_profile_from_documents(profile_id, documents[0], documents[1])
