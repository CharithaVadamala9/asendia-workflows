"""Built-in workflow modules.

Importing this package registers every module. The frontend's palette and its
configuration forms are both derived from what registers here, so adding a module is a
backend-only change.
"""

from app.modules import (  # noqa: F401
    ai_phone_call,
    approval_gate,
    assessment_report,
    jobdiva_writeback,
    new_applicants,
    outreach,
    resume_screening,
)
