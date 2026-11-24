// GitHub Actions(regression.yml)와 같은 파이프라인을 Jenkins로 옮긴 버전.
// 필요 플러그인: Pipeline, JUnit, HTML Publisher. 에이전트에 python3.12, node 필요.

pipeline {
    agent any

    parameters {
        string(name: 'MARKER', defaultValue: '', description: "pytest -m 식 (예: smoke). 비우면 전체")
        string(name: 'INJECT_BUGS', defaultValue: '', description: '회귀 훈련: pagination,idempotency,unread')
    }

    triggers {
        cron('H 3 * * *') // 야간 회귀
    }

    environment {
        INJECT_BUGS = "${params.INJECT_BUGS}"
    }

    options {
        timeout(time: 20, unit: 'MINUTES')
        disableConcurrentBuilds()
    }

    stages {
        stage('Setup') {
            steps {
                sh '''
                    python3.12 -m venv .venv
                    . .venv/bin/activate
                    pip install -q -r requirements.txt
                '''
            }
        }

        stage('Lint') {
            steps {
                sh '. .venv/bin/activate && ruff check . && ruff format --check .'
            }
        }

        stage('Regression') {
            parallel {
                stage('pytest: scenarios') {
                    steps {
                        sh """. .venv/bin/activate && pytest tests/test_scenarios.py -m '${params.MARKER}' \
                            --junitxml=reports/scenarios.xml --html=reports/scenarios.html --self-contained-html"""
                    }
                }
                stage('pytest: behavior') {
                    steps {
                        sh """. .venv/bin/activate && pytest tests/test_messaging.py -m '${params.MARKER}' \
                            --junitxml=reports/behavior.xml --html=reports/behavior.html --self-contained-html"""
                    }
                }
                stage('newman') {
                    steps {
                        sh '''
                            . .venv/bin/activate
                            uvicorn app.main:app --port 8765 --log-level warning & echo $! > .server.pid
                            timeout 15 bash -c 'until curl -sf http://127.0.0.1:8765/health; do sleep 0.2; done'
                            npx -y -p newman@6.2.1 -p newman-reporter-htmlextra@1.23.1 newman run postman/messaging-regression.postman_collection.json \
                                --env-var baseUrl=http://127.0.0.1:8765 \
                                -r cli,junit,htmlextra --reporter-junit-export reports/newman.xml \
                                --reporter-htmlextra-export reports/newman.html || STATUS=$?
                            kill $(cat .server.pid)
                            exit ${STATUS:-0}
                        '''
                    }
                }
            }
        }
    }

    post {
        always {
            junit allowEmptyResults: true, testResults: 'reports/*.xml'
            publishHTML(target: [reportDir: 'reports', reportFiles: 'scenarios.html,behavior.html,newman.html',
                                 reportName: 'Regression Report', keepAll: true, allowMissing: true])
            sh 'python3 tools/summarize_junit.py reports/*.xml > reports/summary.md || true'
            archiveArtifacts artifacts: 'reports/**', allowEmptyArchive: true
        }
    }
}
