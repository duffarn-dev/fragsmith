# Two-PC setup

Rendering (step 4) needs a Windows PC with CS2 running in the foreground, and that PC is busy while it renders. If you have a second PC, you can let it do the rendering while you keep using your main PC.

With only one PC you don't need any of this: keep `[render].mode = "local"` and `render` runs CS2 on your own PC.

`python main.py setup` asks the same questions as this page and writes the config for you. `python main.py doctor` then checks the connection to the render PC.

- **Main PC:** everything except rendering (parsing, AI Director, editing, music).
- **Render PC:** runs `worker.py`, which picks up render jobs from a job folder, launches CS2 through CS Demo Manager and HLAE, and puts the raw clips back.

## Requirements on the render PC

1. **CS2**, installed through Steam
2. **CS Demo Manager.** It installs to `%LOCALAPPDATA%\Programs\cs-demo-manager` by default, where `csdm.exe` lives. HLAE and FFmpeg are installed automatically by csdm on first run.
3. **Python 3.10+** from python.org. `worker.py` only uses the standard library, so no pip packages are needed.
4. **worker.py**, copied from this project to the render PC

## Good to know about CS Demo Manager

- Open the CS Demo Manager app **once** first and finish the setup wizard. It sets up the PostgreSQL database the CLI depends on.
- Rendering starts CS2 with `-insecure`. That's normal, but don't play VAC-secured matches while the worker is running.
- The PC is **busy** during rendering because CS2 runs in the foreground. Don't touch it.
- CS2 updates break HLAE from time to time. Symptom: `csdm video` fails right after a CS2 update. Fix: update HLAE in CS Demo Manager (**Settings → Video → HLAE**; updating CS Demo Manager itself doesn't) and try again. The worker stops by itself after 2 failures in a row.

## Choose how jobs travel between the PCs

**Option A: SSH (recommended).** The main PC sends jobs and fetches clips itself. Enable the OpenSSH server on the render PC, set up key-based login, and fill in `config.toml` on the main PC:

```toml
[render]
mode = "ssh"
ssh_host = "user@192.168.1.50"   # privacy-check: ignore
ssh_key = 'C:\Users\<you>\.ssh\fragsmith_render'
remote_job_dir = "C:/fragsmith-jobs"
```

**Option B: shared folder.**
1. Share a folder on the render PC (e.g. `C:\fragsmith-jobs`): right-click → Properties → Sharing → share with your user.
2. On the main PC, set the path in `config.toml`:
   ```toml
   [render]
   mode = "folder"
   job_dir = '\\RENDER-PC-NAME\fragsmith-jobs'
   ```

**Option C: USB.** Set `mode = "folder"` and leave `job_dir` empty. The `render` command then writes the job locally and tells you exactly what to copy.

## Start the worker

On the render PC:

```
python worker.py --job-dir C:\fragsmith-jobs
```

Add `--csdm "C:\...\csdm.exe"` if csdm lives somewhere unusual, or `--once` to process a single job and exit.

The worker logs to the console and to `<job_dir>\worker.log`. A job that fails is moved to `<job_dir>\failed\`, with its result (and the error) in `outbox\`. To retry it, move it back to `inbox\`. A running worker doesn't pick up code changes: restart it after updating `worker.py`. After updating Fragsmith on the main PC, copy the new `worker.py` to the render PC too. `python main.py selftest` warns when the worker there is older (its results lack the version info).

## End-to-end flow

```
main PC:    python main.py run <demo.dem>      → edit plan + pause for approval
main PC:    python main.py render runs/<id>    → job + demo into the queue
render PC:  worker.py picks up the job         → CS2 renders the clips
main PC:    python main.py finish runs/<id>    → fetches clips, music, final.mp4
main PC:    python main.py cleanup runs/<id>   → delete raw clips (after you've
                                                  watched the film)
```
