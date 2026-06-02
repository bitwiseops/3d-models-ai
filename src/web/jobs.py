"""JobManager: registry in-memory di task background per la web UI.

Modello: ogni `submit()` parte un Future nel ThreadPoolExecutor condiviso.
Ogni Job ha un `progress` (0-100), un `log` (lista di stringhe step) e uno
`status` (pending/running/done/failed). Gli aggiornamenti settano un
`asyncio.Event` per-job per svegliare i subscriber SSE.
"""
from __future__ import annotations

import asyncio
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Literal

JobKind = Literal[
    "image", "feedback", "accept", "portrait", "portrait_bust_3d", "scad",
    "breakdown", "component_regen",
]
JobStatus = Literal["pending", "running", "done", "failed"]


@dataclass
class JobRecord:
    job_id: str
    session_id: str
    kind: JobKind
    status: JobStatus = "pending"
    progress: int = 0
    progress_status: str = ""  # ultimo status Meshy ("IN_PROGRESS" ecc.)
    log: list[str] = field(default_factory=list)
    result: Any = None
    error: str | None = None
    started_at: float = 0.0
    ended_at: float | None = None

    def snapshot(self) -> dict:
        """Versione serializzabile per SSE."""
        return {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "progress_status": self.progress_status,
            "log_tail": self.log[-5:],  # solo ultime 5 righe
            "error": self.error,
        }


class JobManager:
    def __init__(self, executor: ThreadPoolExecutor, loop: asyncio.AbstractEventLoop):
        self._executor = executor
        self._loop = loop
        self._jobs: dict[str, JobRecord] = {}
        # Un Event per job: setta() ogni volta che il record viene aggiornato,
        # i subscriber SSE await sull'event e poi lo clear()-ano.
        self._events: dict[str, asyncio.Event] = {}
        self._guard = asyncio.Lock()

    def _event_for(self, job_id: str) -> asyncio.Event:
        ev = self._events.get(job_id)
        if ev is None:
            ev = asyncio.Event()
            self._events[job_id] = ev
        return ev

    def submit(
        self,
        session_id: str,
        kind: JobKind,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> str:
        """Sottomette `fn(*args, **kwargs)` al ThreadPoolExecutor. Ritorna job_id."""
        job_id = uuid.uuid4().hex[:12]
        record = JobRecord(
            job_id=job_id,
            session_id=session_id,
            kind=kind,
            started_at=time.time(),
        )
        self._jobs[job_id] = record
        # Event creato qui per consistenza, accessed dal loop event-driven
        self._events[job_id] = asyncio.Event()

        # Costruisce i callback PRIMA di submit, così la fn target li riceve.
        def on_progress(pct: int, status: str) -> None:
            self._signal(job_id, progress=pct, progress_status=status)

        def on_step(label: str) -> None:
            self._signal(job_id, log_append=label)

        # Inietta i callback solo se la fn li accetta (introspection light).
        import inspect

        sig = inspect.signature(fn)
        if "on_progress" in sig.parameters:
            kwargs.setdefault("on_progress", on_progress)
        if "on_step" in sig.parameters:
            kwargs.setdefault("on_step", on_step)

        record.status = "running"
        future: Future = self._executor.submit(fn, *args, **kwargs)
        future.add_done_callback(lambda f: self._on_done(job_id, f))
        return job_id

    def _signal(
        self,
        job_id: str,
        *,
        progress: int | None = None,
        progress_status: str | None = None,
        log_append: str | None = None,
        status: JobStatus | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        """Aggiorna un JobRecord e sveglia i subscriber SSE.

        Chiamato da WORKER thread (non dal loop). Usa `call_soon_threadsafe`
        per setare l'asyncio.Event in modo safe.
        """
        rec = self._jobs.get(job_id)
        if rec is None:
            return
        if progress is not None:
            rec.progress = progress
        if progress_status is not None:
            rec.progress_status = progress_status
        if log_append is not None:
            rec.log.append(log_append)
        if status is not None:
            rec.status = status
        if result is not None:
            rec.result = result
        if error is not None:
            rec.error = error
        if status in ("done", "failed"):
            rec.ended_at = time.time()
        # Wake SSE subscribers. Guard: il loop può essere già chiuso se
        # il server è in shutdown mentre un worker thread sta finendo.
        ev = self._events.get(job_id)
        if ev is not None:
            try:
                self._loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                # Loop chiuso: niente subscriber da svegliare, lo stato del
                # JobRecord è comunque aggiornato in memoria.
                pass

    def _on_done(self, job_id: str, future: Future) -> None:
        """Callback del Future: chiamato dal worker thread. Aggiorna stato finale."""
        exc = future.exception()
        if exc is not None:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            self._signal(job_id, status="failed", error=f"{exc}\n\n{tb[-500:]}")
        else:
            self._signal(
                job_id, status="done", progress=100, result=str(future.result())
            )

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    def list_by_session(self, session_id: str) -> list[JobRecord]:
        return [r for r in self._jobs.values() if r.session_id == session_id]

    def active_for_session(self, session_id: str) -> JobRecord | None:
        """Ritorna il job in corso (running/pending) per la sessione, se esiste."""
        for r in self._jobs.values():
            if r.session_id == session_id and r.status in ("pending", "running"):
                return r
        return None

    def active_global(self) -> JobRecord | None:
        """Ritorna il job in corso più recente, fra tutte le sessioni."""
        for r in reversed(list(self._jobs.values())):
            if r.status in ("pending", "running"):
                return r
        return None

    async def subscribe(self, job_id: str) -> AsyncIterator[dict]:
        """Async generator che yield-a snapshot ad ogni cambiamento del job.
        Termina quando lo stato diventa done/failed. Garantisce almeno uno
        snapshot iniziale + uno finale.
        """
        rec = self._jobs.get(job_id)
        if rec is None:
            yield {"error": "job_not_found"}
            return
        # Yield iniziale
        yield rec.snapshot()
        while rec.status in ("pending", "running"):
            ev = self._event_for(job_id)
            try:
                await asyncio.wait_for(ev.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                # Heartbeat: yield snapshot anche senza cambio per tenere alive la connessione SSE
                yield rec.snapshot()
                continue
            ev.clear()
            yield rec.snapshot()
        # Snapshot finale assicurato
        yield rec.snapshot()
