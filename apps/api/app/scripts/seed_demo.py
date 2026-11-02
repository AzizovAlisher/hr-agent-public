"""Demo data seeder.

Wipes existing positions / candidates / scores / chat sessions / messages,
then inserts a varied set of positions and candidates with realistic scores.
LLM calls are skipped — comments are pre-baked in Russian to match v1's tone.

Usage:
    uv run python -m app.scripts.seed_demo
"""

import asyncio
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app.db import session_scope
from app.engine.parsing import calculate_overall_score
from app.models.candidate import Candidate
from app.models.chat_session import ChatSession
from app.models.ai_hiring import AiOrganizationSettings
from app.models.message import Message
from app.models.position import Position
from app.models.score import Score
from app.models.user import User


# ---------- Position fixtures ----------


@dataclass
class PositionFixture:
    title: str
    description: str
    requirements: dict
    status: str
    age_days: int  # how long ago it was "updated"


POSITIONS: list[PositionFixture] = [
    PositionFixture(
        title="Senior Backend разработчик",
        description=(
            "Ищем опытного бэкенд-разработчика для финтех-продукта. Стек: "
            "Python, FastAPI, PostgreSQL, Redis, Kubernetes. Высоконагруженная "
            "система, обработка платежей, требования к безопасности банковского уровня."
        ),
        requirements={
            "python": "5+ years",
            "fastapi": "required",
            "postgres": "required",
            "kubernetes": "preferred",
        },
        status="open",
        age_days=0,
    ),
    PositionFixture(
        title="Data Scientist (NLP)",
        description=(
            "Опыт с трансформерами, fine-tuning LLM, классификация текстов на "
            "русском языке. Production-ready решения, работа с большими корпусами."
        ),
        requirements={
            "transformers": "required",
            "llm-finetuning": "preferred",
            "russian-nlp": "required",
        },
        status="open",
        age_days=0,
    ),
    PositionFixture(
        title="Frontend Developer (React)",
        description=(
            "Next.js 14+, TypeScript, Tailwind. Дизайн-ориентированный продукт, "
            "ценим внимание к деталям и UX. Опыт с дизайн-системами обязателен."
        ),
        requirements={
            "react": "5+ years",
            "next.js": "required",
            "typescript": "required",
        },
        status="open",
        age_days=1,
    ),
    PositionFixture(
        title="DevOps инженер",
        description=(
            "Kubernetes, Terraform, AWS. Работа с инфраструктурой банковского "
            "уровня, CI/CD pipelines, observability."
        ),
        requirements={
            "kubernetes": "required",
            "terraform": "required",
            "aws": "required",
        },
        status="paused",
        age_days=3,
    ),
    PositionFixture(
        title="ML Engineer",
        description=(
            "Production ML systems, MLOps, опыт деплоя моделей. Bonus: "
            "компьютерное зрение или генеративные модели."
        ),
        requirements={
            "mlops": "required",
            "pytorch": "required",
            "kubernetes": "preferred",
        },
        status="open",
        age_days=4,
    ),
    PositionFixture(
        title="Product Designer (Senior)",
        description=(
            "B2B SaaS, complex enterprise dashboards. Сильное портфолио, "
            "опыт работы с дизайн-системами и user research."
        ),
        requirements={
            "figma": "required",
            "design-systems": "required",
            "b2b-saas": "preferred",
        },
        status="open",
        age_days=7,
    ),
    PositionFixture(
        title="iOS Developer (Swift)",
        description=(
            "Swift, SwiftUI, опыт работы с offline-first приложениями. "
            "Mobile-first продукт с миллионом MAU."
        ),
        requirements={
            "swift": "5+ years",
            "swiftui": "required",
        },
        status="open",
        age_days=10,
    ),
    PositionFixture(
        title="Engineering Manager",
        description=(
            "Управление командой 10-15 человек, опыт менторства, hands-on "
            "технический бэкграунд. Распределённая команда."
        ),
        requirements={
            "leadership": "5+ years",
            "team-size": "10+",
        },
        status="closed",
        age_days=21,
    ),
    PositionFixture(
        title="QA Automation Engineer",
        description=(
            "Pytest, Playwright, опыт с CI/CD test pipelines. Покрытие "
            "критичных user flows автотестами."
        ),
        requirements={
            "pytest": "required",
            "playwright": "preferred",
        },
        status="open",
        age_days=2,
    ),
]


# ---------- Candidate fixtures ----------


@dataclass
class CandidateFixture:
    source: str
    source_ref: str
    raw_cv: str
    parsed_cv: dict | None
    # scores per criterion + comments per criterion
    technical_skills: int
    quality_of_projects: int
    previous_experience: int
    impact: int
    comment_technical_skills: str
    comment_quality_of_projects: str
    comment_previous_experience: str
    comment_impact: str
    model_used: str = "google/gemini-2.5-flash"
    days_ago: int = 0
    is_favorite: bool = False
    viewed_hours_ago: int | None = None  # if set, candidate has been viewed


# A bank of candidates; we'll attach them to specific positions below.
CANDIDATES: dict[str, list[CandidateFixture]] = {
    "Senior Backend разработчик": [
        CandidateFixture(
            source="hh",
            source_ref="resume_71029384",
            raw_cv=(
                "Senior Backend Engineer · 7 лет опыта\n\n"
                "Опыт работы:\n"
                "2022 — наст. время · Tech Lead · Fintech Platform Inc.\n"
                "— Руководство командой из 5 backend-разработчиков\n"
                "— Архитектура и разработка платёжного движка (FastAPI, PostgreSQL, Redis, Kafka)\n"
                "— Снижение p99 latency на 60% через оптимизацию запросов и кеширование\n"
                "— Интеграции с банками-партнёрами (ISO 20022, SWIFT)\n"
                "— Внедрение CI/CD pipelines (GitLab CI, ArgoCD)\n\n"
                "2019 — 2022 · Senior Backend Engineer · E-commerce Solutions\n"
                "— Миграция монолитного приложения на микросервисы\n"
                "— Разработка catalog и order-management сервисов (50k RPS пиковая нагрузка)\n"
                "— Введение event-driven архитектуры (Kafka)\n\n"
                "2017 — 2019 · Backend Developer · StartupTech\n"
                "— Python/Django, PostgreSQL, Celery\n"
                "— Разработка API для мобильных приложений\n\n"
                "Стек: Python, FastAPI, Django, PostgreSQL, Redis, Kafka, Kubernetes, "
                "Docker, AWS, GitLab CI, Prometheus, Grafana, ELK\n\n"
                "Образование: МГТУ им. Баумана, Прикладная математика, 2016"
            ),
            parsed_cv={
                "id": "71029384",
                "title": "Senior Backend Engineer",
                "experience_years": 7,
                "skills": ["Python", "FastAPI", "PostgreSQL", "Kubernetes", "Redis", "Kafka"],
                "current_role": "Tech Lead",
                "team_size": 5,
            },
            technical_skills=96,
            quality_of_projects=93,
            previous_experience=91,
            impact=95,
            comment_technical_skills=(
                "Уверенно владеет Python, FastAPI, PostgreSQL. Глубокое понимание "
                "async patterns, опыт с Kubernetes на production уровне."
            ),
            comment_quality_of_projects=(
                "Архитектурно зрелые проекты: микросервисная платформа обработки "
                "платежей с нагрузкой 50k RPS, грамотное использование очередей и кеширования."
            ),
            comment_previous_experience=(
                "7 лет коммерческого опыта, последние 3 года — fintech, "
                "обработка платежей и интеграции с банками."
            ),
            comment_impact=(
                "Тимлид команды из 5 человек. Оптимизация снизила p99 latency на 60%, "
                "экономия инфраструктуры $40k/год."
            ),
            days_ago=0,
            is_favorite=True,
            viewed_hours_ago=3,
        ),
        CandidateFixture(
            source="hh",
            source_ref="resume_64812743",
            raw_cv=(
                "Backend Engineer · 5 лет опыта\n\n"
                "Текущее место: E-commerce Solutions, Senior Backend Engineer\n"
                "— Python, Django, FastAPI (последний год)\n"
                "— PostgreSQL на продвинутом уровне, опыт с шардированием\n"
                "— Опыт работы с большой нагрузкой во время распродаж (Black Friday)\n"
                "— Менторство 2 джуниоров\n\n"
                "До этого: StartupCloud, Backend Developer\n"
                "— Python/FastAPI, PostgreSQL, Redis\n"
                "— Разработка SaaS-платформы для логистики"
            ),
            parsed_cv=None,
            technical_skills=90,
            quality_of_projects=85,
            previous_experience=88,
            impact=84,
            comment_technical_skills=(
                "Сильные навыки в Python и Django, опыт с FastAPI год, "
                "PostgreSQL на продвинутом уровне."
            ),
            comment_quality_of_projects=(
                "Качественные проекты в e-commerce, опыт работы с большой "
                "нагрузкой во время распродаж."
            ),
            comment_previous_experience=(
                "5 лет, последние 2 — в международной e-commerce компании. "
                "Без fintech, но смежная домен-экспертиза."
            ),
            comment_impact=(
                "Senior IC, рефакторинг legacy-системы, миграция на микросервисы. "
                "Менторство джунов."
            ),
            days_ago=0,
            is_favorite=True,
            viewed_hours_ago=12,
        ),
        CandidateFixture(
            source="avito",
            source_ref="avito_2891034",
            raw_cv=(
                "Middle+ Backend Developer\n"
                "— Python, FastAPI базовый, PostgreSQL\n"
                "— 4.5 года опыта в разных компаниях\n"
                "— Разнообразный опыт от стартапов до средних компаний\n"
                "— CRUD-приложения, REST API, базовая интеграция с внешними сервисами"
            ),
            parsed_cv=None,
            technical_skills=78,
            quality_of_projects=72,
            previous_experience=80,
            impact=73,
            comment_technical_skills=(
                "Python на хорошем уровне, FastAPI базовый. Опыт с PostgreSQL "
                "без глубокой оптимизации."
            ),
            comment_quality_of_projects=(
                "Средние по сложности проекты, в основном CRUD-приложения. "
                "Не хватает опыта с распределёнными системами."
            ),
            comment_previous_experience=(
                "4.5 года, разнообразный опыт от стартапов до средних компаний. Без fintech."
            ),
            comment_impact=(
                "Middle+ разработчик, работа в команде, без явной leadership-роли. "
                "Стабильный исполнитель."
            ),
            model_used="anthropic/claude-3.5-sonnet",
            days_ago=1,
        ),
        CandidateFixture(
            source="manual",
            source_ref="upload_4f2a91e8",
            raw_cv=(
                "Junior Python Developer\n"
                "— 1.5 года в небольшой студии\n"
                "— Базовый Python, нет опыта с FastAPI\n"
                "— PostgreSQL поверхностно\n"
                "— Учебные и pet-проекты в портфолио"
            ),
            parsed_cv=None,
            technical_skills=58,
            quality_of_projects=48,
            previous_experience=54,
            impact=50,
            comment_technical_skills=(
                "Базовый Python, нет опыта с FastAPI. Знаком с PostgreSQL "
                "поверхностно. Подходит для junior-позиций."
            ),
            comment_quality_of_projects=(
                "Учебные и pet-проекты, без production-опыта. Простая архитектура "
                "без масштабирования."
            ),
            comment_previous_experience=(
                "1.5 года коммерческого опыта в небольшой студии. Не соответствует "
                "уровню senior."
            ),
            comment_impact=(
                "Junior IC, работа по спецификациям, без самостоятельных "
                "архитектурных решений."
            ),
            days_ago=3,
        ),
        CandidateFixture(
            source="hh",
            source_ref="resume_55672901",
            raw_cv=(
                "Backend Engineer · 6 лет\n\n"
                "Текущее место: BankCore Systems, Backend Engineer\n"
                "— Python, FastAPI, PostgreSQL, RabbitMQ\n"
                "— Разработка core banking сервисов, обработка транзакций\n"
                "— SLA 99.9%, мониторинг через Prometheus + Alertmanager\n\n"
                "До этого: PayTech, Backend Developer\n"
                "— Платёжные интеграции, антифрод"
            ),
            parsed_cv=None,
            technical_skills=88,
            quality_of_projects=86,
            previous_experience=89,
            impact=82,
            comment_technical_skills=(
                "Python и FastAPI на хорошем уровне, опыт с очередями (RabbitMQ). "
                "Не хватает Kubernetes на production."
            ),
            comment_quality_of_projects=(
                "Серьёзные проекты в банковском секторе, обработка транзакций "
                "с высокими требованиями к надёжности."
            ),
            comment_previous_experience=(
                "6 лет в финансовом секторе — идеально подходит для fintech-домена. "
                "Опыт с регуляторными требованиями."
            ),
            comment_impact=(
                "Senior IC роль, без leadership-опыта, но глубокая экспертиза "
                "в платёжных системах."
            ),
            days_ago=2,
        ),
    ],
    "Frontend Developer (React)": [
        CandidateFixture(
            source="hh",
            source_ref="resume_88123456",
            raw_cv=(
                "Senior Frontend Engineer · 6 лет\n"
                "— Next.js 14+, TypeScript, Tailwind, React Query\n"
                "— Опыт с дизайн-системами (Storybook, design tokens)\n"
                "— Запуск трёх production B2B SaaS продуктов\n"
                "— Менторство 3 разработчиков\n"
                "— Сильный фокус на performance и accessibility"
            ),
            parsed_cv=None,
            technical_skills=94,
            quality_of_projects=90,
            previous_experience=92,
            impact=88,
            comment_technical_skills=(
                "Глубокая экспертиза в современном React stack. Сильное понимание "
                "Next.js App Router и React Server Components."
            ),
            comment_quality_of_projects=(
                "Производственный B2B SaaS опыт, дизайн-системы Storybook. "
                "Внимание к performance и a11y."
            ),
            comment_previous_experience=(
                "6 лет, последние 4 — на TypeScript. Опыт работы в продуктовых "
                "командах и стартапах."
            ),
            comment_impact=(
                "Менторство трёх разработчиков, code reviews, инициатор "
                "внедрения дизайн-системы."
            ),
            days_ago=1,
            is_favorite=True,
        ),
        CandidateFixture(
            source="manual",
            source_ref="upload_a7c4b3d2",
            raw_cv=(
                "Frontend Developer · 4 года\n"
                "— React, Vue, JavaScript (TypeScript последний год)\n"
                "— Работа в outsource-команде\n"
                "— Опыт с rest API, базовая работа с Redux/Zustand\n"
                "— Знаком с Tailwind"
            ),
            parsed_cv=None,
            technical_skills=72,
            quality_of_projects=68,
            previous_experience=75,
            impact=65,
            comment_technical_skills=(
                "React на хорошем уровне. Опыт с TypeScript ограничен последним "
                "годом. Next.js не упоминается."
            ),
            comment_quality_of_projects=(
                "Outsource-проекты средней сложности. Не хватает опыта с "
                "продуктовой разработкой."
            ),
            comment_previous_experience=(
                "4 года в outsource. Переход в продуктовую команду потребует "
                "адаптации."
            ),
            comment_impact=(
                "Исполнитель в outsource-модели, без явной самостоятельности "
                "в технических решениях."
            ),
            days_ago=2,
        ),
        CandidateFixture(
            source="hh",
            source_ref="resume_91047283",
            raw_cv=(
                "Frontend Engineer · 3 года\n"
                "— React, TypeScript, Next.js (Pages Router в основном)\n"
                "— Работа в небольшой стартап-команде\n"
                "— Опыт интеграции с GraphQL backend"
            ),
            parsed_cv=None,
            technical_skills=80,
            quality_of_projects=76,
            previous_experience=70,
            impact=68,
            comment_technical_skills=(
                "Хороший React/TypeScript, опыт с Next.js (Pages Router). "
                "App Router и RSC — пробелы."
            ),
            comment_quality_of_projects=(
                "Стартап-проекты, MVP-уровень. Дизайн-систему не выстраивал."
            ),
            comment_previous_experience=(
                "3 года, мало для senior-позиции. Junior+/middle уровень."
            ),
            comment_impact=(
                "Самостоятельный middle-разработчик, без leadership и "
                "менторства."
            ),
            days_ago=1,
        ),
    ],
    "DevOps инженер": [
        CandidateFixture(
            source="hh",
            source_ref="resume_44829103",
            raw_cv=(
                "Senior DevOps Engineer · 8 лет\n"
                "— Kubernetes (production), Terraform, AWS, GCP\n"
                "— Опыт с банковской инфраструктурой, compliance, SOC 2\n"
                "— GitLab CI, ArgoCD, Prometheus, Grafana, ELK\n"
                "— Менторство и tech leadership"
            ),
            parsed_cv=None,
            technical_skills=92,
            quality_of_projects=89,
            previous_experience=94,
            impact=87,
            comment_technical_skills=(
                "Глубокая экспертиза в Kubernetes и Terraform. Multi-cloud опыт."
            ),
            comment_quality_of_projects=(
                "Серьёзные банковские проекты с требованиями по compliance. "
                "SOC 2 audit experience."
            ),
            comment_previous_experience=(
                "8 лет в DevOps, последние 4 — банковский сектор. Идеально "
                "подходит под требования."
            ),
            comment_impact=(
                "Tech leadership, менторство. Внедрение GitOps подхода в "
                "командах."
            ),
            days_ago=4,
            is_favorite=True,
            viewed_hours_ago=48,
        ),
        CandidateFixture(
            source="hh",
            source_ref="resume_72918374",
            raw_cv=(
                "DevOps Engineer · 5 лет\n"
                "— Kubernetes, базовый Terraform, AWS\n"
                "— CI/CD на Jenkins и GitHub Actions\n"
                "— Без опыта в банковском секторе"
            ),
            parsed_cv=None,
            technical_skills=78,
            quality_of_projects=72,
            previous_experience=70,
            impact=68,
            comment_technical_skills=(
                "Kubernetes на хорошем уровне, Terraform базовый. Нужно "
                "углубление в IaC."
            ),
            comment_quality_of_projects=(
                "Средние проекты, в основном веб-сервисы. Compliance и "
                "регуляторные требования — пробелы."
            ),
            comment_previous_experience=(
                "5 лет, разный опыт. Не подходит банковский домен."
            ),
            comment_impact=(
                "Самостоятельный DevOps, без leadership-опыта."
            ),
            days_ago=5,
        ),
    ],
    "Data Scientist (NLP)": [
        CandidateFixture(
            source="hh",
            source_ref="resume_93847261",
            raw_cv=(
                "ML Engineer / NLP · 5 лет\n"
                "— PyTorch, HuggingFace transformers, fine-tuning LLM\n"
                "— Работа с русскоязычными корпусами (RuBERT, ruGPT)\n"
                "— Production deployments через TorchServe и BentoML\n"
                "— Публикации на ACL и EMNLP"
            ),
            parsed_cv=None,
            technical_skills=93,
            quality_of_projects=91,
            previous_experience=87,
            impact=85,
            comment_technical_skills=(
                "Глубокая экспертиза в transformers и fine-tuning. "
                "Опыт с production ML."
            ),
            comment_quality_of_projects=(
                "Серьёзные NLP-проекты на русском языке, академические "
                "публикации."
            ),
            comment_previous_experience=(
                "5 лет в ML, последние 3 — NLP. Хорошо подходит."
            ),
            comment_impact=(
                "Tech contributor, без явной leadership-роли."
            ),
            days_ago=0,
        ),
        CandidateFixture(
            source="manual",
            source_ref="upload_e8f2c1a7",
            raw_cv=(
                "Junior Data Scientist · 1 год\n"
                "— Базовый Python, scikit-learn, начинающий PyTorch\n"
                "— Pet-проекты с классификацией текстов\n"
                "— PhD в лингвистике"
            ),
            parsed_cv=None,
            technical_skills=52,
            quality_of_projects=45,
            previous_experience=40,
            impact=42,
            comment_technical_skills=(
                "Junior уровень, нет production-опыта с LLM. Сильный "
                "академический бэкграунд в лингвистике."
            ),
            comment_quality_of_projects=(
                "Только pet-проекты, без масштабирования."
            ),
            comment_previous_experience=(
                "1 год опыта недостаточно для роли."
            ),
            comment_impact=(
                "Junior, нет самостоятельных проектов."
            ),
            days_ago=2,
        ),
    ],
}


# ---------- main seed ----------


async def main() -> None:
    async with session_scope() as db:
        # Make sure an admin exists; we won't fail if not (positions can have created_by=NULL).
        admin = (
            await db.execute(select(User).where(User.role == "admin").limit(1))
        ).scalar_one_or_none()
        admin_id = admin.id if admin else None
        # Every seeded row goes into the admin's org. If there's no admin yet
        # (fresh DB), fall back to the migration's default org UUID.
        org_id = (
            admin.organization_id
            if admin
            else uuid.UUID("00000000-0000-0000-0000-000000000001")
        )

        # The AI product is opt-in for existing organizations. The demo
        # organization is intentionally enabled so `/ai` can be exercised
        # immediately after seeding without changing production defaults.
        ai_settings = (
            await db.execute(
                select(AiOrganizationSettings).where(
                    AiOrganizationSettings.organization_id == org_id
                )
            )
        ).scalar_one_or_none()
        if ai_settings is None:
            db.add(
                AiOrganizationSettings(
                    organization_id=org_id,
                    feature_enabled=True,
                )
            )
        else:
            ai_settings.feature_enabled = True

        # Wipe
        await db.execute(delete(Message))
        await db.execute(delete(ChatSession))
        await db.execute(delete(Score))
        await db.execute(delete(Candidate))
        await db.execute(delete(Position))

        now = datetime.now(UTC)

        # Pre-baked notes for a couple positions to demo the feature
        notes_for: dict[str, str] = {
            "Senior Backend разработчик": (
                "Hiring manager хочет видеть 2 финалистов к среде. "
                "Команда в Москве, удалённый формат с заездами раз в квартал. "
                "Бюджет: 350-450k gross."
            ),
            "Frontend Developer (React)": (
                "В этой роли важен дизайн-восприимчивый кандидат — отдельный фокус "
                "на портфолио и code style. Спросить на интервью про Storybook опыт."
            ),
        }

        positions_by_title: dict[str, Position] = {}
        for fp in POSITIONS:
            updated = now - timedelta(days=fp.age_days)
            p = Position(
                id=uuid.uuid4(),
                organization_id=org_id,
                title=fp.title,
                description=fp.description,
                requirements=fp.requirements,
                internal_notes=notes_for.get(fp.title),
                status=fp.status,
                created_by=admin_id,
                created_at=updated,
                updated_at=updated,
            )
            db.add(p)
            positions_by_title[fp.title] = p
        await db.flush()

        n_candidates = 0
        n_scores = 0
        for position_title, fixtures in CANDIDATES.items():
            position = positions_by_title.get(position_title)
            if not position:
                continue
            for fc in fixtures:
                created = now - timedelta(days=fc.days_ago, hours=fc.days_ago)
                viewed = (
                    now - timedelta(hours=fc.viewed_hours_ago)
                    if fc.viewed_hours_ago is not None
                    else None
                )
                candidate = Candidate(
                    id=uuid.uuid4(),
                    organization_id=org_id,
                    source=fc.source,
                    source_ref=fc.source_ref,
                    raw_cv=fc.raw_cv,
                    parsed_cv=fc.parsed_cv,
                    pdf_path=None,
                    is_favorite=fc.is_favorite,
                    viewed_at=viewed,
                    created_at=created,
                )
                db.add(candidate)
                await db.flush()
                n_candidates += 1

                criteria = {
                    "technical_skills": fc.technical_skills,
                    "quality_of_projects": fc.quality_of_projects,
                    "previous_experience": fc.previous_experience,
                    "impact": fc.impact,
                }
                overall = calculate_overall_score(criteria)
                score = Score(
                    id=uuid.uuid4(),
                    organization_id=org_id,
                    candidate_id=candidate.id,
                    position_id=position.id,
                    score_technical_skills=fc.technical_skills,
                    score_quality_of_projects=fc.quality_of_projects,
                    score_previous_experience=fc.previous_experience,
                    score_impact=fc.impact,
                    comment_technical_skills=fc.comment_technical_skills,
                    comment_quality_of_projects=fc.comment_quality_of_projects,
                    comment_previous_experience=fc.comment_previous_experience,
                    comment_impact=fc.comment_impact,
                    overall_score=overall,
                    model_used=fc.model_used,
                    scored_at=created,
                    owner_user_id=admin.id,
                )
                db.add(score)
                n_scores += 1

        print(
            f"Seeded {len(POSITIONS)} positions, {n_candidates} candidates, "
            f"{n_scores} scores."
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
