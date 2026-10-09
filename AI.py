import numpy as np
import random


TASK_COUNT=5
class worker:
    def __init__(self,worker_id):
        self.worker_id=worker_id
        self.skill=[random.random() for _ in range(5)]
        self.year=random.randrange(0,30)
        self.learning_rate=0.001

    def update_skill(self,task_type):
        self.skill[task_type]+=self.learning_rate*(1-self.skill[task_type])

    def get_context(self,taskdifficulty,task_type):
        year_norm=self.year/30
        tasklist=[0]*TASK_COUNT
        tasklist[task_type]=1
        context=[year_norm,taskdifficulty,year_norm*taskdifficulty]+tasklist
        return context

    def simulate_output(self ,task_type):
        time_taken=1000/(self.skill[task_type]+np.random.normal(loc=1,scale=0.1))#一旦秒とする
        feedback_list=[0,1,2] #0:easy 1:normal 2:hard
        feedback=random.choice(feedback_list)
        return time_taken,feedback #feedbackは仮
    
class RewardConverter:
    def __init__(self):
        self.feedback_reward=[1.5,1.0,0.5]
        self.task_avgtime_list=np.zeros(TASK_COUNT)
        self.tasked_count=np.zeros(TASK_COUNT)
        self.learningdata=[0]*TASK_COUNT

    def to_reward(self,feedback,time_taken,task_type):
        """
        feedback: 0=easy/1=normal/2=hard が分かっていれば渡す（本人の体感申告など）。
        None なら過去平均との比較から自動推定する。
        以前は feedback を渡しても必ず時間から再計算されて握りつぶされていたが、
        それだと呼び出し側が feedback を渡す意味が無いので、渡された値を優先する。

        比較対象の平均は「今回のサンプルを含める前」の値を使う。先に平均へ
        混ぜてしまうと、今回が外れ値でも基準自体がその場で引きずられて
        必ず"normal"寄りに判定されてしまうため。
        """
        baseline=self.task_avgtime_list[task_type] if self.tasked_count[task_type]>0 else time_taken
        time_score=baseline/time_taken
        if feedback is None:
            if baseline*0.9>time_taken:
                feedback=0
            elif baseline*1.1<time_taken:
                feedback=2
            else:
                feedback=1
        reward=self.feedback_reward[feedback]*time_score
        self.update_avg(task_type,time_taken)  # 平均は評価に使った後で更新する
        self.recordReward(task_type,reward)
        return reward

    def update_avg(self,task_type,taken_time):
        self.tasked_count[task_type]+=1
        current_count=self.tasked_count[task_type]
        current_avg=self.task_avgtime_list[task_type]
        avg=(current_avg*(current_count-1)+taken_time)/current_count
        self.task_avgtime_list[task_type]=avg
        return avg

    def recordReward(self,choicetask,reward):
        # update_avg が先にカウントを進めているので、ここでの current_count は
        # 「今回を含めた件数」。重みは update_avg と同じく current_count-1 にする
        # （そうしないと過去の平均が二重に効いて発散する）。
        current_count=self.tasked_count[choicetask]
        prev_avg=self.learningdata[choicetask]
        self.learningdata[choicetask]=(prev_avg*(current_count-1)+reward)/current_count

class workerBelief:
    """
    ベイズ線形回帰の事後分布。精度行列 A = Σ⁻¹ と b = A·μ の形で持つ。

        A ← A + xxᵀ/σ²,  b ← b + r·x/σ²

    以前は update のたびに逆行列を3回取っていたが、この形なら更新は足し算だけで
    済む。μ と Σ は読まれたときに1回だけ逆行列を取って作る（結果は同じ）。
    ai_stub は実績を全件再生して組み立て直すので、ここが再学習の速さを決める。
    """
    def __init__(self,dim=3+TASK_COUNT):
        # dim は文脈ベクトルの次元。単体シミュレーションは既定の8次元、
        # ai_stub.py は権限・機材の one-hot を足した次元で作る
        self.sigma_obs2=0.1
        self._A=np.eye(dim)/100     # 事前分布 Σ=100·I の精度
        self._b=np.zeros(dim)       # 事前分布 μ=0
        self._mu=None
        self._sigma=None

    def _solve(self):
        if self._sigma is None:
            self._sigma=np.linalg.inv(self._A)
            self._mu=self._sigma@self._b

    @property
    def mu(self):
        self._solve()
        return self._mu

    @property
    def sigma(self):
        self._solve()
        return self._sigma

    def sample_theta(self):
        return np.random.multivariate_normal(self.mu,self.sigma)

    def predict(self,x,theta):
        return float(np.dot(theta,x))  #x,thetaは同じ次元なら何次元でもよい

    def update(self,x,reward):
        x=np.asarray(x,dtype=float)
        self._A+=np.outer(x,x)/self.sigma_obs2
        self._b+=x*(reward/self.sigma_obs2)
        self._mu=self._sigma=None
        

class AssignmentEngine:
    def __init__(self,n_workers=5):
        # 以前は5人分をベタ書きしていたため、n_workers を変えると
        # choice_worker() が IndexError になっていた。worker数に合わせて作る。
        self.beliefs=[workerBelief() for _ in range(n_workers)]

    def choice_worker(self,x,theta,i):
        return self.beliefs[i].predict(x,theta)



class Simulator:
    def __init__(self,n_workers=5):
        self.workers=[worker(i) for i in range(n_workers)]
        self.task_difficulty=[0.15,0.3,0.45,0.6,0.85]       #タスク難易度1~5#タスク数５に固定
        self.step_count=0
        self.current_task_type=None
    def get_context(self):
        contexts=[]
        self.current_task_type=random.randrange(len(self.task_difficulty))
        for i in range(len(self.workers)):
            difficulty=self.task_difficulty[self.current_task_type]
            contexts.append(self.workers[i].get_context(difficulty,self.current_task_type))
        return contexts,self.current_task_type



if __name__ == "__main__":
    # ここから下は AI.py 単体で回す検証用シミュレーション。
    # app.py からは ai_stub.py が上のクラスだけを import する。
    sim=Simulator()
    matrix = [[0 for _ in range(5)] for _ in range(5)]
    r=RewardConverter()
    ae=AssignmentEngine(n_workers=len(sim.workers))
    #elo=elorating(n_workers=5,n_task_type=3)
    feedbacked=["easy","normal","hard"]
    for i in range(10000):
        choicetask=random.randrange(0,5)
        j=0
        index=0
        max_predict=float('-inf')
        for workers in sim.workers:
            predict= ae.choice_worker(workers.get_context(sim.task_difficulty[choicetask],choicetask),ae.beliefs[j].sample_theta(),j)
            if max_predict<predict:
                max_predict=predict
                index=j
            j=j+1
        matrix[choicetask][index]=matrix[choicetask][index]+1
        timetaken,feedback=sim.workers[index].simulate_output(choicetask)
        reward=r.to_reward(feedback,timetaken,choicetask)
        ae.beliefs[index].update(sim.workers[index].get_context(sim.task_difficulty[choicetask],choicetask),reward)
        sim.workers[index].update_skill(choicetask)  # 選ばれて作業したので熟練度を上げる
        print(f"選択タスク{choicetask}")
        print(f"選択された人{index}")




    for w in sim.workers:
        print("各workerSkill")
        print(w.worker_id, w.skill)
    for i, belief in enumerate(ae.beliefs):
        print("各workerMu")
        print(i, belief.mu)
    print("選ばれた回数,matrix[choicetask][index]")
    print(matrix)

