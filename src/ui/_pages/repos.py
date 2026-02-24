from datetime import UTC

import streamlit as st

from src.ui.helpers import api_delete, api_get, api_post, status_badge, time_ago


def render():
    col_title, col_refresh = st.columns([6, 1])
    with col_title:
        st.header("Repository Manager")
    with col_refresh:
        st.write("")
        if st.button("🔄 Refresh", key="repos_refresh_top"):
            st.rerun()

    # -------------------------------------------------------------------------
    # Section 1 — Registered Repositories
    # -------------------------------------------------------------------------
    st.subheader("Registered Repositories")

    repos_data, repos_err = api_get("/repos", timeout=10)

    if repos_err:
        st.error(f"Failed to load repositories: {repos_err}")
    elif not repos_data:
        st.info("No repositories registered yet. Use the form below to add one.")
    else:
        for repo in repos_data:
            owner = repo.get("owner", "")
            name = repo.get("name", "")
            repo_slug = repo.get("repo", f"{owner}/{name}")
            branch = repo.get("branch", "main")
            status = repo.get("status", "unknown")
            active_chunks = repo.get("active_chunks", 0)
            deleted_chunks = repo.get("deleted_chunks", 0)
            files = repo.get("files", 0)
            symbols = repo.get("symbols", 0)
            last_indexed_raw = repo.get("last_indexed")
            last_indexed_str = time_ago(last_indexed_raw) if last_indexed_raw else "Never"

            webhook_hook_id = repo.get("webhook_hook_id")
            webhook_registered = repo.get("webhook_registered", False)
            confirm_key = f"confirm_delete_{owner}_{name}"

            with st.container(border=True):
                left_col, right_col = st.columns([3, 1])

                with left_col:
                    st.markdown(f"**{repo_slug}**")
                    st.markdown(
                        f"Branch: `{branch}` &nbsp;|&nbsp; Status: {status_badge(status)}",
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f"Chunks: **{active_chunks}** (deleted: {deleted_chunks}) &nbsp;|&nbsp; "
                        f"Files: **{files}** &nbsp;|&nbsp; "
                        f"Symbols: **{symbols}**",
                        unsafe_allow_html=True,
                    )
                    if webhook_registered:
                        st.markdown(
                            f"Webhook: <span style='color:green'>registered (hook #{webhook_hook_id})</span>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            "Webhook: <span style='color:orange'>not registered</span>",
                            unsafe_allow_html=True,
                        )
                    st.caption(f"Last indexed: {last_indexed_str}")

                with right_col:
                    btn_reindex, btn_webhook, btn_remove = st.columns(3)

                    with btn_reindex:
                        if st.button("🔄 Re-index", key=f"reindex_{owner}_{name}"):
                            with st.spinner(f"Triggering re-index for {repo_slug}…"):
                                data, err = api_post(
                                    f"/repos/{owner}/{name}/index",
                                    json={},
                                    timeout=30,
                                )
                            if err:
                                st.error(f"Re-index failed: {err}")
                            else:
                                job_id = (data or {}).get("job_id", "")
                                short_id = job_id[:8] if job_id else "unknown"
                                files_found = (data or {}).get("files_found", 0)
                                st.success(
                                    f"Re-index job queued — **{files_found} files** "
                                    f"(Job ID: `{short_id}`)"
                                )

                    with btn_webhook:
                        if not webhook_registered:
                            if st.button("🔗 Setup Webhook", key=f"webhook_{owner}_{name}"):
                                with st.spinner("Registering webhook…"):
                                    data, err = api_post(
                                        f"/repos/{owner}/{name}/webhook",
                                        json={},
                                        timeout=15,
                                    )
                                if err:
                                    st.error(f"Webhook setup failed: {err}")
                                elif data and data.get("success"):
                                    st.success(data.get("message", "Webhook registered!"))
                                    st.rerun()
                                else:
                                    st.warning(data.get("message", "Could not register webhook."))
                                    instructions = (data or {}).get("manual_instructions")
                                    if instructions:
                                        st.info(instructions)
                        else:
                            if st.button("🔗 Remove Webhook", key=f"rm_webhook_{owner}_{name}"):
                                with st.spinner("Removing webhook…"):
                                    data, err = api_delete(
                                        f"/repos/{owner}/{name}/webhook",
                                        timeout=15,
                                    )
                                if err:
                                    st.error(f"Remove failed: {err}")
                                else:
                                    st.success((data or {}).get("message", "Webhook removed."))
                                    st.rerun()

                    with btn_remove:
                        if not st.session_state.get(confirm_key, False):
                            if st.button("🗑️ Remove", key=f"remove_{owner}_{name}"):
                                st.session_state[confirm_key] = True
                                st.rerun()
                        else:
                            st.warning("⚠️ This deletes all indexed data. Confirm?")
                            yes_col, cancel_col = st.columns(2)
                            with yes_col:
                                if st.button(
                                    "Yes, delete",
                                    key=f"confirm_yes_{owner}_{name}",
                                    type="primary",
                                ):
                                    with st.spinner(f"Deleting {repo_slug}…"):
                                        data, err = api_delete(
                                            f"/repos/{owner}/{name}",
                                            timeout=15,
                                        )
                                    st.session_state.pop(confirm_key, None)
                                    if err:
                                        st.error(f"Delete failed: {err}")
                                    else:
                                        st.success(f"{repo_slug} removed successfully.")
                                    st.rerun()
                            with cancel_col:
                                if st.button(
                                    "Cancel",
                                    key=f"confirm_cancel_{owner}_{name}",
                                ):
                                    st.session_state.pop(confirm_key, None)
                                    st.rerun()

    st.divider()

    # -------------------------------------------------------------------------
    # Section 2 — Add Repository form
    # -------------------------------------------------------------------------
    st.subheader("Add Repository")

    with st.form("add_repo_form"):
        owner_input = st.text_input("Owner", placeholder="myorg")
        name_input = st.text_input("Repository Name", placeholder="my-repo")
        branch_input = st.text_input("Branch", value="main")
        description_input = st.text_input("Description (optional)", placeholder="")
        start_indexing = st.checkbox("Start indexing immediately after registration", value=True)
        submitted = st.form_submit_button("➕ Register Repository")

    if submitted:
        if not owner_input.strip():
            st.error("Owner is required.")
        elif not name_input.strip():
            st.error("Repository name is required.")
        else:
            owner_clean = owner_input.strip()
            name_clean = name_input.strip()
            branch_clean = branch_input.strip() or "main"

            payload = {
                "owner": owner_clean,
                "name": name_clean,
                "branch": branch_clean,
                "description": description_input.strip(),
            }

            with st.spinner(f"Registering {owner_clean}/{name_clean}…"):
                reg_data, reg_err = api_post("/repos", json=payload, timeout=15)

            if reg_err:
                st.error(f"Registration failed: {reg_err}")
            else:
                st.success(f"Repository {owner_clean}/{name_clean} registered successfully.")

                # Show webhook auto-registration result
                webhook_info = (reg_data or {}).get("webhook", {})
                if webhook_info.get("success"):
                    st.success(f"🔗 {webhook_info.get('message', 'Webhook registered!')}")
                elif webhook_info:
                    st.warning(f"🔗 Webhook: {webhook_info.get('message', 'Could not auto-register.')}")
                    instructions = webhook_info.get("manual_instructions")
                    if instructions:
                        st.info(instructions)

                if start_indexing:
                    with st.spinner("Triggering initial index job…"):
                        idx_data, idx_err = api_post(
                            f"/repos/{owner_clean}/{name_clean}/index",
                            json={},
                            timeout=30,
                        )
                    if idx_err:
                        st.error(f"Indexing could not be started: {idx_err}")
                    else:
                        job_id = (idx_data or {}).get("job_id", "")
                        short_id = job_id[:8] if job_id else "unknown"
                        files_found = (idx_data or {}).get("files_found", 0)
                        st.success(
                            f"✅ Indexing job queued — **{files_found} files** to index "
                            f"(Job ID: `{short_id}`)"
                        )
                        st.info(
                            "⚙️ **The RQ worker must be running to process this job.** "
                            "If chunks stay at 0, start it with:\n\n"
                            "```\nPYTHONPATH=. OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES "
                            "rq worker indexing --url redis://localhost:6379\n```",
                            icon="ℹ️",
                        )

                st.rerun()

    st.divider()

    # -------------------------------------------------------------------------
    # Section 3 — Recent Indexing Jobs
    # -------------------------------------------------------------------------
    st.subheader("Recent Indexing Jobs")

    jobs_data, jobs_err = api_get("/jobs", timeout=10)

    if jobs_err:
        st.error(f"Failed to load jobs: {jobs_err}")
    else:
        jobs_list = (jobs_data or {}).get("jobs", [])
        queued_count = (jobs_data or {}).get("queued_count", 0)

        if queued_count > 0:
            # Check if any queued job has been waiting > 30s without being picked up
            from datetime import datetime

            stale = False
            for job in jobs_list:
                if (
                    job.get("state") == "queued"
                    and job.get("enqueued_at")
                    and not job.get("started_at")
                ):
                    try:
                        enqueued = datetime.fromisoformat(job["enqueued_at"].replace("Z", "+00:00"))
                        age_s = (datetime.now(UTC) - enqueued).total_seconds()
                        if age_s > 30:
                            stale = True
                            break
                    except Exception:
                        pass

            if stale:
                st.warning(
                    f"⚠️ **{queued_count} job(s) queued but the RQ worker is not running.** "
                    "Start it to begin indexing:\n\n"
                    "```\nPYTHONPATH=. OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES "
                    "rq worker indexing --url redis://localhost:6379\n```"
                )
            else:
                st.info(f"{queued_count} job(s) currently in queue")

        if not jobs_list:
            st.info("No indexing jobs found.")
        else:
            recent_jobs = jobs_list[-10:][::-1]

            table_rows = []
            for job in recent_jobs:
                job_id_full = job.get("id", "")
                short_id = job_id_full[:8] if job_id_full else "—"
                state = job.get("state", "unknown")

                enqueued_raw = job.get("enqueued_at") or job.get("enqueued")
                enqueued_str = time_ago(enqueued_raw) if enqueued_raw else "—"

                started_raw = job.get("started_at") or job.get("started")
                ended_raw = job.get("ended_at") or job.get("ended")

                duration_str = "—"
                if started_raw and ended_raw:
                    try:
                        from datetime import datetime

                        def _parse(ts):
                            ts = ts.rstrip("Z")
                            if "+" in ts:
                                ts = ts.split("+")[0]
                            return datetime.fromisoformat(ts).replace(tzinfo=UTC)

                        delta = _parse(ended_raw) - _parse(started_raw)
                        duration_str = f"{delta.total_seconds():.1f}s"
                    except Exception:
                        duration_str = "—"

                result = job.get("result") or {}
                files_processed = "—"
                errors_count = "—"
                if state == "finished" and isinstance(result, dict):
                    fp = result.get("files_processed") or result.get("files")
                    if fp is not None:
                        files_processed = str(fp)
                    ec = result.get("errors") or result.get("error_count")
                    if ec is not None:
                        errors_count = str(ec)

                table_rows.append(
                    {
                        "Job ID": short_id,
                        "State": status_badge(state),
                        "Enqueued": enqueued_str,
                        "Duration": duration_str,
                        "Files Processed": files_processed,
                        "Errors": errors_count,
                    }
                )

            # Render as markdown table since status_badge returns HTML
            header = "| Job ID | State | Enqueued | Duration | Files Processed | Errors |"
            separator = "|--------|-------|----------|----------|-----------------|--------|"
            rows_md = [header, separator]
            for row in table_rows:
                rows_md.append(
                    f"| {row['Job ID']} | {row['State']} | {row['Enqueued']} "
                    f"| {row['Duration']} | {row['Files Processed']} | {row['Errors']} |"
                )

            st.markdown("\n".join(rows_md), unsafe_allow_html=True)
