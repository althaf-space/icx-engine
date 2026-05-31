import axios from 'axios';

const BASE = '/api/v1';

export const getOrders = () => axios.get(`${BASE}/orders`);
export const createOrder = (data) => axios.post(`${BASE}/orders`, data);
export const getOrder = (id) => axios.get(`${BASE}/orders/${id}`);
export const getUsers = () => axios.get(`${BASE}/users`);
