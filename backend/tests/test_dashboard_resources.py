from pathlib import Path
import pytest
from shmax.dashboard import ResourceHistory,group_summary
from tests.test_multi_group import rt,seed


def test_cpu_deltas_memory_and_cgroup_are_distinct_and_history_bounded(tmp_path):
    proc=tmp_path/'proc';cg=tmp_path/'cg';proc.mkdir();cg.mkdir()
    (proc/'stat').write_text('cpu 100 0 100 800 0 0 0 0 0 0\n')
    (proc/'meminfo').write_text('MemTotal: 10000 kB\nMemAvailable: 6000 kB\n')
    (proc/'loadavg').write_text('0.40 0.20 0.10 1/100 1\n')
    (cg/'memory.current').write_text('1024');(cg/'memory.max').write_text('2048')
    (cg/'cpu.max').write_text('100000 100000');(cg/'cpu.stat').write_text('usage_usec 1000000\n')
    now=[100]
    history=ResourceHistory(tmp_path,proc=proc,cgroup=cg,clock=lambda:now[0],capacity=2)
    first=history.collect();assert first['cpu_percent'] is None
    assert first['memory_percent']==40 and first['memory_total']==10000*1024
    assert first['container_memory_used']==1024 and first['container_memory_limit']==2048
    now[0]=105;(proc/'stat').write_text('cpu 125 0 125 850 0 0 0 0\n')
    (cg/'cpu.stat').write_text('usage_usec 3500000\n')
    second=history.collect()
    assert second['cpu_percent']==50 and second['container_cpu_percent']==50
    for sample in (first,second,second):history.samples.append(sample)
    assert len(history.snapshot()['samples'])==2


def test_unavailable_linux_metrics_are_null_not_fabricated_zero(tmp_path):
    history=ResourceHistory(tmp_path,proc=tmp_path/'absent',cgroup=tmp_path/'absent')
    data=history.collect()
    assert data['cpu_percent'] is None and data['memory_percent'] is None
    assert data['disk_total']>0 and history.snapshot()['scope']=='unavailable'

@pytest.mark.asyncio
async def test_dashboard_groups_include_independent_readiness_and_admin_mode(rt):
    await seed(rt)
    overview=await group_summary(rt.db)
    assert len(overview['groups'])==3
    assert [g['routes'] for g in overview['groups']]==[1,1,0]
    assert [g['admin_hub'] for g in overview['groups']]==[False,False,True]
