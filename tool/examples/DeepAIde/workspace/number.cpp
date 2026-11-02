#include<bits/stdc++.h>
using namespace std;
#define ll unsigned long long
int T,m,x,num[10],cnt;
ll n,l,r,ans,inf=(ll)1e18+1;
set<int> a;
bool check(ll x){
	ll res=0,b=1;
	while(x){
		res=min(res+num[x%cnt]*b,inf);
		x/=cnt,b=min(b*10,inf);
	}
	return res<=n;
}
int main(){
	freopen("number.in","r",stdin);
	freopen("number.out","w",stdout);
	scanf("%d",&T);
	while(T--){
		scanf("%lld%d",&n,&m);
		a.clear();
		while(m--){
			scanf("%d",&x);
			a.insert(x);
		}
		cnt=0;
		for(int i=0;i<10;i++)
			if(!a.count(i))
				num[cnt++]=i;
		l=ans=0,r=1e18;
		while(l<=r){
			ll mid=l+r>>1;
			if(check(mid)) ans=mid,l=mid+1;
			else r=mid-1;
		}
		printf("%lld\n",ans+1);
	}
}