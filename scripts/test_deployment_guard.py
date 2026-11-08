"""Read-only process-control simulations for the deployment ingress guard."""
import signal
import unittest
from unittest.mock import patch
import deployment_guard as guard


class GuardTests(unittest.TestCase):
    def test_descendants_include_children_of_non_main_threads_and_deduplicate(self):
        paths={'/proc/10/task':['10','11'],'/proc/20/task':['20'],'/proc/30/task':['30']}
        children={'/proc/10/task/10/children':'','/proc/10/task/11/children':'20 30',
                  '/proc/20/task/20/children':'30','/proc/30/task/30/children':''}
        def iterdir(path):
            if str(path) not in paths:raise FileNotFoundError()
            return iter(path/value for value in paths[str(path)])
        with patch.object(guard.Path,'iterdir',iterdir), patch.object(guard.Path,'read_text',lambda path:children[str(path)]):
            self.assertEqual(sorted(guard.descendants(10)),[20,30])

    def test_pauses_only_ingress_and_mcp_not_download_descendants(self):
        paused=[]
        def pause(pid,handles):
            paused.append(pid);handles.append(pid+1000);return True
        with patch.object(guard,'main_pid',return_value=10), patch.object(guard,'descendants',return_value=[20,30,40]), patch.object(guard,'is_mcp_server',side_effect=lambda p:p==20), patch.object(guard,'pause',side_effect=pause), patch.object(guard.signal,'pidfd_send_signal') as send, patch.object(guard.os,'close') as close:
            with guard.paused_ingress():pass
        self.assertEqual(paused,[10,20])
        self.assertEqual([c.args for c in send.call_args_list],[(1020,signal.SIGCONT),(1010,signal.SIGCONT)])
        self.assertEqual(close.call_count,2)

    def test_busy_job_abort_resumes_original_processes(self):
        def pause(pid,handles):handles.append(7);return True
        with patch.object(guard,'main_pid',return_value=10), patch.object(guard,'descendants',return_value=[]), patch.object(guard,'pause',side_effect=pause), patch.object(guard.signal,'pidfd_send_signal') as send, patch.object(guard.os,'close'):
            with self.assertRaisesRegex(RuntimeError,'active jobs'):
                with guard.paused_ingress():raise RuntimeError('active jobs')
            send.assert_called_once_with(7,signal.SIGCONT)

    def test_service_replacement_aborts_before_deployment(self):
        def pause(pid,handles):handles.append(7);return True
        with patch.object(guard,'main_pid',side_effect=[10,11]), patch.object(guard,'descendants',return_value=[]), patch.object(guard,'pause',side_effect=pause), patch.object(guard.signal,'pidfd_send_signal') as send, patch.object(guard.os,'close'):
            with self.assertRaisesRegex(RuntimeError,'restarted'):
                with guard.paused_ingress():self.fail('must not run deployment')
            send.assert_called_once_with(7,signal.SIGCONT)

    def test_exited_original_process_is_not_resumed_by_pid(self):
        def pause(pid,handles):handles.append(7);return True
        with patch.object(guard,'main_pid',return_value=10), patch.object(guard,'descendants',return_value=[]), patch.object(guard,'pause',side_effect=pause), patch.object(guard.signal,'pidfd_send_signal',side_effect=ProcessLookupError), patch.object(guard.os,'close') as close:
            with guard.paused_ingress():pass
            close.assert_called_once_with(7)

    def test_failed_stop_still_resumes_earlier_handles(self):
        def pause(pid,handles):
            handles.append(pid)
            if pid==20:raise RuntimeError('pause failed')
            return True
        with patch.object(guard,'main_pid',return_value=10), patch.object(guard,'descendants',return_value=[20]), patch.object(guard,'is_mcp_server',return_value=True), patch.object(guard,'pause',side_effect=pause), patch.object(guard.signal,'pidfd_send_signal') as send, patch.object(guard.os,'close'):
            with self.assertRaisesRegex(RuntimeError,'pause failed'):
                with guard.paused_ingress():self.fail('must not run deployment')
            self.assertEqual([c.args[0] for c in send.call_args_list],[20,10])


if __name__=='__main__':unittest.main()
