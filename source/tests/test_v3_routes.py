"""Version-three routes retain local origin, token and confirmation checks."""
from unittest.mock import patch
from tests.test_app import ApiFixture


class WorkspaceRoutesTests(ApiFixture):
    def test_file_tools_recovery_and_cloud_action_routes_require_authorization(self):
        routes=['/api/scan/interrupted-resume','/api/tools/plan','/api/tools/execute','/api/tools/history',
                '/api/tools/undo','/api/tools/keepers','/api/cloud/action-plan','/api/cloud/action-execute',
                '/api/cloud/action-restore']
        with patch.object(self.server.app.cloud,'execute_action') as execute, patch('app.scan_folder') as scan:
            for route in routes:
                with self.subTest(route=route):
                    self.assertEqual(self.request('POST',route,{},authorized=False)[0],403)
                    self.assertEqual(self.request('POST',route,{},headers={'Origin':'https://invalid.example'})[0],403)
                    self.assertEqual(self.request('POST',route,{},headers={'Content-Type':'text/plain'})[0],415)
            execute.assert_not_called();scan.assert_not_called()

    def test_new_interface_assets_are_served_locally(self):
        for route in ['/static/workspace.js','/static/workspace.css','/static/file_tools.js','/static/file_tools.css']:
            self.assertEqual(self.request('GET',route)[0],200,route)

    def test_cloud_action_arguments_dispatch_without_implicit_confirmation(self):
        cloud=self.server.app.cloud
        with patch.object(cloud,'action_plan',return_value={'planId':'generated'}) as plan:
            status,result=self.request('POST','/api/cloud/action-plan',{'ids':['a'],'action':'year','destination':'Generated'})
            self.assertEqual(status,200,result)
            plan.assert_called_once_with(['a'],'year','Generated')
        with patch.object(cloud,'execute_action',return_value={'ok':True}) as execute:
            self.request('POST','/api/cloud/action-execute',{'planId':'generated'})
            execute.assert_called_once_with('generated',False)
        with patch.object(cloud,'restore_action',return_value={'ok':True}) as restore:
            self.request('POST','/api/cloud/action-restore',{'operationId':'generated','confirmed':True})
            restore.assert_called_once_with('generated',True)

    def test_csv_report_route_contains_generated_group_paths(self):
        (self.root/'a.txt').write_text('Generated CSV duplicate','utf-8')
        (self.root/'b.txt').write_text('Generated CSV duplicate','utf-8')
        self.scan()
        status,body=self.request('GET','/api/report.csv')
        self.assertEqual(status,200)
        text=body.decode('utf-8-sig')
        self.assertIn('Full path',text);self.assertIn('a.txt',text);self.assertIn('b.txt',text)

    def test_tools_plan_cannot_execute_without_explicit_confirmation(self):
        (self.root/'a.txt').write_text('Generated rename fixture','utf-8')
        self.scan()
        status,plan=self.request('POST','/api/tools/plan',{'kind':'rename','template':'renamed_{counter:04d}'})
        self.assertEqual(status,200,plan)
        self.assertEqual(self.request('POST','/api/tools/execute',{'planId':plan['planId']})[0],400)
        self.assertTrue((self.root/'a.txt').exists())
        status,result=self.request('POST','/api/tools/execute',{'planId':plan['planId'],'confirmed':True})
        self.assertEqual(status,200,result)
        self.assertTrue((self.root/'renamed_0001.txt').exists())
        status,result=self.request('POST','/api/tools/undo',{'operationId':result['operationId'],'confirmed':True})
        self.assertEqual(status,200,result)
        self.assertTrue((self.root/'a.txt').exists())
