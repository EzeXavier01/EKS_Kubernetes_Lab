import http from 'k6/http';
export const options = {
  stages: [
    { duration: '1m', target: 50 },
    { duration: '3m', target: 50 },
    { duration: '1m', target: 0 },
  ],
};
export default function () {
  http.get(`http://${__ENV.LB}/`);
}
