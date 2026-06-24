# stub — TranslatorEngine — implemented in M1-12


class TranslatorEngine:
    """Public API for the Borgésica translation engine. All methods raise NotImplementedError
    until the corresponding milestone task implements them."""

    def create_job(self, source_path: str, config: object) -> object:
        raise NotImplementedError

    def estimate_cost(self, job_id: str) -> object:
        raise NotImplementedError

    def run_job(self, job_id: str, on_progress: object = None) -> object:
        raise NotImplementedError

    def resume_job(self, job_id: str, on_progress: object = None) -> object:
        raise NotImplementedError

    def status(self, job_id: str) -> object:
        raise NotImplementedError

    def get_glossary(self, job_id: str) -> object:
        raise NotImplementedError

    def update_glossary(self, job_id: str, entries: list) -> object:
        raise NotImplementedError

    def cancel_job(self, job_id: str) -> None:
        raise NotImplementedError
