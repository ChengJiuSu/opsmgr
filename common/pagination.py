from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 200

    def paginate_queryset(self, queryset, request, view=None):
        # page_size=0 → 跳过分页一次性返回全量（导出/同步类场景），
        # 响应结构与分页时一致（total/page/page_size/results），前端与脚本无需区分
        if request.query_params.get(self.page_size_query_param) == '0':
            self.request = request
            self._full_result = list(queryset)
            return self._full_result
        self._full_result = None
        return super().paginate_queryset(queryset, request, view)

    def get_paginated_response(self, data):
        if getattr(self, '_full_result', None) is not None:
            return Response({
                'code': 0,
                'data': {
                    'total': len(data),
                    'page': 1,
                    'page_size': len(data),
                    'results': data,
                },
            })
        return Response({
            'code': 0,
            'data': {
                'total': self.page.paginator.count,
                'page': self.page.number,
                'page_size': self.get_page_size(self.request),
                'results': data,
            },
        })

    def get_paginated_response_schema(self, schema):
        return {
            'type': 'object',
            'properties': {
                'code': {'type': 'integer'},
                'data': {
                    'type': 'object',
                    'properties': {
                        'total': {'type': 'integer'},
                        'page': {'type': 'integer'},
                        'page_size': {'type': 'integer'},
                        'results': schema,
                    },
                },
            },
        }
