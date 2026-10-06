import pytest
from crawlers.careers.models import Job
from crawlers.careers.store import Store

@pytest.fixture
def store():
    return Store(":memory:")

def job(job_id: str, company: str = "Acme", title: str = "Software Engineer Intern", **kw) -> Job:
    return Job(company=company, ats="greenhouse", job_id=job_id, title=title,
               url=f"https://example.com/{job_id}", **kw)
