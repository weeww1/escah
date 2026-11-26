"""Deduplicate automatic collection attempts after the weekly Wednesday deadline."""
import datetime as dt
import json
import os
import re
import urllib.parse
import urllib.request

WORKFLOW = 'character-profiles.yml'
COMPLETED_STEPS = {'Collect public profiles using the platform login',
                   'Commit validated public metadata', 'Mirror validated public metadata to Gitee'}


def weekly_deadline(now):
    now = now.astimezone(dt.timezone.utc)
    deadline = now.replace(hour=10, minute=0, second=0, microsecond=0)
    deadline -= dt.timedelta(days=(now.weekday() - 2) % 7)
    return deadline if deadline <= now else deadline - dt.timedelta(days=7)


def completed_collection(jobs):
    for job in jobs:
        steps = {step.get('name') for step in job.get('steps', [])
                 if step.get('status') == 'completed' and step.get('conclusion') == 'success'}
        if job.get('conclusion') == 'success' and COMPLETED_STEPS <= steps:
            return True
    return False


def decide(now, event, check_schedule, runs, jobs):
    if event != 'schedule' and not check_schedule:
        return {'run': True, 'reason': 'explicit-manual-request'}
    deadline = weekly_deadline(now)
    for row in runs(deadline):
        if row.get('conclusion') != 'success':
            continue
        created = dt.datetime.fromisoformat(row['created_at'].replace('Z', '+00:00'))
        if created >= deadline and completed_collection(jobs(row['id'])):
            return {'run': False, 'reason': 'weekly-collection-already-completed',
                    'completedRun': row['id'], 'deadline': deadline.isoformat()}
    return {'run': True, 'reason': 'weekly-collection-due', 'deadline': deadline.isoformat()}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def main():
    env = os.environ
    repository = env.get('GITHUB_REPOSITORY', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid collection repository')
    token = env.get('GH_TOKEN', '')
    if not token or any(ord(char) < 32 for char in token):
        raise ValueError('Missing GitHub Actions token')
    opener = urllib.request.build_opener(NoRedirect())

    def api(path):
        request = urllib.request.Request('https://api.github.com/repos/' + repository + path,
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                     'User-Agent': 'weekly-profile-collection', 'X-GitHub-Api-Version': '2022-11-28'})
        with opener.open(request, timeout=20) as response:
            return json.load(response)

    def runs(deadline):
        for page in range(1, 6):
            query = urllib.parse.urlencode({'status': 'success', 'created': '>=' + deadline.isoformat(),
                                            'per_page': 100, 'page': page})
            rows = api('/actions/workflows/' + WORKFLOW + '/runs?' + query)['workflow_runs']
            yield from rows
            if len(rows) < 100:
                return
        raise RuntimeError('Too many runs to verify the weekly collection safely')

    observed_at = dt.datetime.now(dt.timezone.utc)
    result = decide(observed_at, env.get('GITHUB_EVENT_NAME'),
                    env.get('CHECK_WEEKLY_SCHEDULE') == '1', runs,
                    lambda run_id: api('/actions/runs/' + str(run_id) + '/jobs?per_page=100')['jobs'])
    result.update({'event': env.get('GITHUB_EVENT_NAME', ''),
                   'cron': env.get('SCHEDULE_CRON', ''), 'observedAt': observed_at.isoformat(),
                   'runId': env.get('GITHUB_RUN_ID', '')})
    with open(env['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        output.write('run=' + str(result['run']).lower() + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
