"""Проверка самообновления: клон, обновление, откат битого коммита, повторная попытка на новом, остановка по SIGTERM."""
import os, signal, subprocess, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def sh(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, code, msg):
    (repo / 'bot.py').write_text(code)
    (repo / 'requirements.txt').write_text('# нет зависимостей\n')
    sh('git', 'add', '-A', cwd=repo)
    sh('git', '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', msg, cwd=repo)
    return sh('git', 'rev-parse', 'HEAD', cwd=repo)


def wait_for(path, text, t=40):
    end = time.time() + t
    while time.time() < end:
        if path.exists() and text in path.read_text():
            return True
        time.sleep(0.3)
    return False


def test_self_update_and_rollback():
    tmp = Path(tempfile.mkdtemp())
    origin, app, data, log = tmp / 'origin', tmp / 'app', tmp / 'data', tmp / 'log'
    origin.mkdir()
    sh('git', 'init', '-q', '-b', 'main', cwd=origin)
    a = commit(origin, "print('VERSION A', flush=True)\nimport sys, time\ntime.sleep(2)\nsys.exit(42)\n", 'A')
    env = dict(os.environ, APP_DIR=str(app), DATA_DIR=str(data), REPO_URL=f'file://{origin}', BRANCH='main', PYTHON='python3', RETRY_SLEEP='1')
    p = subprocess.Popen([str(ROOT / 'entrypoint.sh')], env=env, stdout=open(log, 'w'), stderr=subprocess.STDOUT)
    try:
        assert wait_for(log, 'VERSION A'), log.read_text()
        b = commit(origin, "def broken(:\n", 'B битый')
        assert wait_for(log, 'откат'), log.read_text()                       # B упал -> вернулся на A
        assert (data / 'blocked_commit').read_text().strip() == b
        notice = (data / 'notice.txt').read_text()
        assert 'не запустилось' in notice and 'SyntaxError' in notice, notice     # в уведомлении есть причина
        assert sh('git', 'rev-parse', 'HEAD', cwd=app) == a
        assert wait_for(log, 'уже не запускался'), log.read_text()           # битый коммит больше не пробуется
        # разовый сбой (например, сеть при старте) не должен вызывать откат
        flag = tmp / 'flag'
        d2 = commit(origin, f"import os, sys, time\nif not os.path.exists({str(flag)!r}):\n    open({str(flag)!r}, 'w').close(); sys.exit(1)\nprint('VERSION D', flush=True)\ntime.sleep(2)\nsys.exit(42)\n", 'D с разовым сбоем')
        assert wait_for(log, 'VERSION D'), log.read_text()
        assert 'пробую ещё раз' in log.read_text() and (data / 'blocked_commit').read_text().strip() == b
        c = commit(origin, "print('VERSION C', flush=True)\nimport time\ntime.sleep(300)\n", 'C хороший')
        assert wait_for(log, 'VERSION C'), log.read_text()                   # новый нормальный коммит применился
        assert sh('git', 'rev-parse', 'HEAD', cwd=app) == c
        t = time.time()
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=15)
        assert time.time() - t < 15 and 'остановлен' in log.read_text()
    finally:
        if p.poll() is None:
            p.kill()


if __name__ == '__main__':
    test_self_update_and_rollback(); print('ok test_self_update_and_rollback')
